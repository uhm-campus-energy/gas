"""
2) Extract gas bill data from the PDFs into TWO CSVs, reading each PDF once:

    gas_acc_extracted.csv    one row per account per bill  -> gas.sng_bills
    gas_meter_extracted.csv  one row per meter per bill    -> gas.sng

Both files go in the output folder (from config.py) and are overwritten on
every run. The PDF parsing logic follows the original SNG gas bill extractor.
Paths and the account CSV's columns come from config.py so this script can run
on any machine.

Account CSV: therms is the bill's own printed account-wide total (the
"Consumption Charge" line), not a sum of individual meters.

Meter CSV: therms is the value PRINTED on the bill for each meter. Two checks
run on every bill:
  1. Each meter: (reading_to - reading_from) * factor is close to printed therms.
  2. Each bill: the meter therms add up to the bill's "Consumption Charge" therms.
Problems are printed as warnings and listed at the end. Rows are still written
so you can look at them before anything is uploaded.

Run order:  2) this script  ->  upload scripts for gas.sng and gas.sng_bills
            (either order)  ->  archive script
"""

import glob
import re
import sys
import tempfile
from pathlib import Path
import pandas as pd
from pypdf import PdfReader

# Needed in order to import config
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import extract_query_to_csv


ACCOUNT_REGEX = r"Account Number\s+(\d+)"
BILLED_REGEX = r"Current Charges\s*\$([0-9,]+\.\d{2})"

# Account-wide total therms, printed once in the "Current Charges" breakdown
# table (page 2) -- this is the WHOLE ACCOUNT's total, not one meter's.
TOTAL_THERMS_REGEX = r"Consumption Charge\s+([\d,]+\.\d{2})"

# One meter row. Meter IDs are 4-7 digits (some are 5 digits, e.g. 57729).
# Groups: meter_id, date_from, reading_from, date_to, reading_to, days,
#         factor, therms
METER_ROW_REGEX = (
    r"\b(\d{4,7})\s+"
    r"(\d{2}/\d{2}/\d{4})\s+"
    r"(\d+)\s+"
    r"(\d{2}/\d{2}/\d{4})\s+"
    r"(\d+)\s+"
    r"(\d+)\s+"
    r"([0-9.]+)\s+"
    r"([\d,]+\.\d{2})"
)

METER_COLUMNS = [
    "meter_id",
    "date_from",
    "read1",
    "date_to",
    "read2",
    "days",
    "factor",
    "therms",
    "account",
    "upload_time",
]

# Meter dials are 5 digits, so a read that goes down means it rolled past 99,999
DIAL_ROLLOVER = 100000

# The bill prints factors rounded to 5 decimals, so (usage x factor) can be off
# the printed therms by a few hundredths. Allow that much before warning.
METER_TOLERANCE = 0.5
BILL_TOLERANCE = 0.5

# Used to check if there's overlapping billing dates
LONG_INTERVAL_DAYS = 40

ACCOUNT_CSV_NAME = "gas_acc_extracted.csv"
METER_CSV_NAME = "gas_meter_extracted.csv"


# ── Reading one PDF ──────────────────────────────────────────────────────

def read_bill(pdf_path):
    """
    Read one PDF once. Returns (account, bill_total_therms, billed_amount,
    raw_meter_rows). Anything not found is None (raw_meter_rows is a list).
    The archive script also uses this to read a bill's account and meters.
    """
    account = None
    bill_total = None
    billed_amount = None
    raw_rows = []

    with open(pdf_path, "rb") as pdf_file:
        reader = PdfReader(pdf_file)

        for page in reader.pages:
            text = page.extract_text() or ""

            if account is None:
                match = re.search(ACCOUNT_REGEX, text)
                if match:
                    account = match.group(1)

            if billed_amount is None:
                match = re.search(BILLED_REGEX, text)
                if match:
                    billed_amount = float(match.group(1).replace(",", ""))

            if bill_total is None:
                match = re.search(TOTAL_THERMS_REGEX, text)
                if match is None:
                    match = re.search(r"Consumption\s+([\d,]+\.\d{2})", text)
                if match:
                    bill_total = float(match.group(1).replace(",", ""))

            # Meter rows can be on any page (large accounts spill onto page 2)
            for row_match in re.finditer(METER_ROW_REGEX, text):
                raw_rows.append(row_match.groups())

    return account, bill_total, billed_amount, raw_rows


# ── Building the rows ────────────────────────────────────────────────────

def build_meter_records(account, raw_rows):
    """Turn raw regex groups into meter row dicts. Returns (records, meter_issues)."""
    records = []
    meter_issues = []

    for meter_id, date_from, read_from, date_to, read_to, days, factor, therms in raw_rows:
        reading_from = int(read_from)
        reading_to = int(read_to)
        usage = reading_to - reading_from
        if usage < 0:
            usage += DIAL_ROLLOVER
        factor = float(factor)
        therms = float(therms.replace(",", ""))

        expected = usage * factor
        if abs(expected - therms) > max(METER_TOLERANCE, 0.001 * therms):
            meter_issues.append(
                f"meter {meter_id}: usage {usage} x factor {factor} = {expected:.2f}, "
                f"but bill prints {therms:.2f}"
            )

        records.append({
            "meter_id": meter_id,
            "date_from": pd.to_datetime(date_from).strftime("%Y-%m-%d"),
            "read1": reading_from,
            "date_to": pd.to_datetime(date_to).strftime("%Y-%m-%d"),
            "read2": reading_to,
            "days": int(days),
            "factor": factor,
            "therms": therms,
            "account": account,
            "upload_time": None,  # blank here; the upload script sets it
        })

    return records, meter_issues


def build_account_row(account, bill_total, billed_amount, raw_rows, meter_sum):
    """
    Build the one gas.sng_bills row for a bill. Returns (row_or_None, notes)
    where notes is a list of messages to print. The row is None if the bill
    doesn't have what gas.sng_bills needs (e.g. no 'Current Charges' amount).
    """
    notes = []

    # Billing period = the first meter row's dates (the same way the original
    # account extractor read them).
    date_from = raw_rows[0][1]
    date_to = raw_rows[0][3]

    total_therms = bill_total
    note = ""
    if total_therms is None:
        # No "Consumption Charge" line: use the meter table instead.
        total_therms = meter_sum
        note = "no consumption charge line; therms from meter table"
        if total_therms == 0:
            notes.append("[INFO] no consumption charge, meter table confirms 0.00 therms")
        else:
            notes.append(
                f"[WARNING] no consumption charge line but meter table sums to "
                f"{total_therms} therms -- please double-check"
            )

    if billed_amount is None:
        return None, notes

    if pd.to_datetime(date_to) - pd.to_datetime(date_from) > pd.Timedelta(days=LONG_INTERVAL_DAYS):
        notes.append(f"Long billing interval detected for account {account}: {date_from} to {date_to}")

    return {
        "account": account,
        "date_from": pd.to_datetime(date_from).strftime("%Y-%m-%d"),
        "date_to": pd.to_datetime(date_to).strftime("%Y-%m-%d"),
        "therms": total_therms,
        "therms_adjusted": total_therms,
        "billed_$": billed_amount,
        "note": note,
    }, notes


# ── Running over every PDF ───────────────────────────────────────────────

def extract_all_bills(input_folder):
    """
    Extract every PDF under input_folder except files in duplicates.
    Returns (account_df, meter_df, problems).
    """
    duplicate_dir = Path(config.DUPLICATE_DIR).resolve()

    all_pdfs = [Path(p) for p in glob.glob(str(Path(input_folder) / "**" / "*.pdf"), recursive=True)]

    pdf_files = []
    skipped_duplicates = 0
    for pdf_path in all_pdfs:
        resolved = pdf_path.resolve()
        if pdf_path.name.startswith("combined_"):
            continue
        if resolved == duplicate_dir or duplicate_dir in resolved.parents:
            skipped_duplicates += 1
            continue
        pdf_files.append(pdf_path)

    total = len(pdf_files)
    print(f"Found {len(all_pdfs)} PDF files total, skipping {skipped_duplicates} in duplicates")

    account_rows = []
    meter_rows = []
    problems = []

    for number, pdf_path in enumerate(pdf_files, start=1):
        label = f"[{number}/{total}] {pdf_path.name}"

        try:
            account, bill_total, billed_amount, raw_rows = read_bill(pdf_path)
        except Exception as error:
            print(f"{label} -> ERROR: {error}")
            problems.append(f"{pdf_path.name}: could not be read ({error})")
            continue

        if account is None or not raw_rows:
            print(f"{label} -> WARNING: no account/meter rows found, skipped")
            problems.append(f"{pdf_path.name}: no account or meter rows found")
            continue

        # Meter rows + checks
        records, meter_issues = build_meter_records(account, raw_rows)
        meter_sum = round(sum(r["therms"] for r in records), 2)
        issues = list(meter_issues)

        if bill_total is None:
            summary = f"{len(records)} meters, {meter_sum:,.2f} therms (no consumption charge line to compare against)"
        elif abs(meter_sum - bill_total) > BILL_TOLERANCE:
            issues.append(
                f"meters add up to {meter_sum:,.2f} therms but the bill total is {bill_total:,.2f}"
            )
            summary = f"{len(records)} meters, WARNING: total mismatch"
        else:
            summary = f"{len(records)} meters, {meter_sum:,.2f} therms, matches bill total"

        # Account row
        account_row, account_notes = build_account_row(
            account, bill_total, billed_amount, raw_rows, meter_sum
        )
        if account_row is None:
            issues.append("no 'Current Charges' amount found, so no gas.sng_bills row was made")
            print(f"{label} -> {summary}; NO account row")
        else:
            print(f"{label} -> account row + {summary}")
            account_rows.append(account_row)

        for note in account_notes:
            print(f"      {note}")
        for issue in issues:
            print(f"      [WARNING] {issue}")
            problems.append(f"{pdf_path.name}: {issue}")

        meter_rows.extend(records)

    print(f"Extracted {len(account_rows)} account rows and {len(meter_rows)} meter rows from {total} PDFs")
    account_df = pd.DataFrame(account_rows, columns=config.COLUMNS)
    meter_df = pd.DataFrame(meter_rows, columns=METER_COLUMNS)
    return account_df, meter_df, problems


def reconcile_long_interval_bills(df, database="uhm2023", table="gas.sng_bills"):
    """
    Find newly extracted bills whose date_from -> date_to interval is
    longer than a normal ~30-day cycle, and split their printed total
    therms evenly across the periods they actually cover.

    This follows the STARS-era approach (added 4/29/25): divide the long
    bill's total therms by the number of monthly periods it spans, and use
    that average as therms_adjusted -- both for the long bill's own row and
    for any already-billed rows in the database that it turns out to
    supersede. It does NOT attempt the old per-meter hand calculation
    described in the data inventory doc -- that's a different, much
    slower process and can stay manual.

    df: the account DataFrame from extract_all_bills(), one row per account
        per newly-parsed PDF (columns: account, date_from, date_to, therms,
        therms_adjusted, billed_$, note).

    Returns (df, updates_df):
        df         -- same DataFrame, with therms_adjusted/note filled in
                      for any long-interval rows found.
        updates_df -- rows already in `table` whose therms_adjusted needs
                      correcting, because a long-interval bill now
                      supersedes them (empty if none found). Feed this into
                      update_existing_bills() to push the correction.
    """
    df = df.copy()
    intervals = pd.to_datetime(df["date_to"]) - pd.to_datetime(df["date_from"])
    long_rows = df[intervals.dt.days > LONG_INTERVAL_DAYS]

    if long_rows.empty:
        return df, pd.DataFrame(columns=df.columns)

    def _append_note(existing, addition):
        existing = str(existing) if pd.notna(existing) and existing else ""
        return (existing + "; " if existing else "") + addition

    all_updates = []

    for idx, row in long_rows.iterrows():
        account, date_from, date_to = row["account"], row["date_from"], row["date_to"]

        # Pull this account's existing bills that fall inside the long
        # bill's span, so the period count comes from actual billing
        # history instead of a day-count guess.
        query = (
            "SELECT account, date_from, date_to, therms, therms_adjusted, "
            f'"billed_$", note FROM {table} '
            f"WHERE account = '{account}' "
            f"AND date_to > '{date_from}' AND date_to <= '{date_to}'"
        )
        overlap_csv = Path(tempfile.gettempdir()) / f"gas_overlap_{account}_{date_to}.csv"
        pulled = extract_query_to_csv(database, query, overlap_csv)
        overlapping = pd.read_csv(overlap_csv) if pulled and overlap_csv.exists() else pd.DataFrame()
        if overlap_csv.exists():
            overlap_csv.unlink()

        if not overlapping.empty:
            period_count = len(overlapping)
        else:
            # No prior bills on file for this stretch (new account, or a
            # gap with nothing billed yet) -- fall back to day count.
            period_count = max(round(intervals[idx].days / 30), 1)
            print(
                f"[WARNING] No bills found in {table} for account {account} "
                f"between {date_from} and {date_to}; estimating "
                f"{period_count} period(s) from day count instead."
            )

        averaged_therms = round(row["therms"] / period_count, 2)
        df.at[idx, "therms_adjusted"] = averaged_therms
        df.at[idx, "note"] = _append_note(row["note"], "used amount due")

        if not overlapping.empty:
            overlapping["therms_adjusted"] = averaged_therms
            overlapping["note"] = overlapping["note"].apply(
                lambda n: _append_note(n, "superseded by long-interval bill")
            )
            all_updates.append(overlapping)

        print(
            f"[INFO] {account}: {date_from} -> {date_to} spans "
            f"{period_count} period(s); therms_adjusted set to {averaged_therms}"
        )

    updates_df = pd.concat(all_updates, ignore_index=True) if all_updates else pd.DataFrame(columns=df.columns)
    return df, updates_df


if __name__ == "__main__":
    print("Starting gas bill extraction from PDFs (account + meter CSVs)...")
    account_df, meter_df, problems = extract_all_bills(config.SOURCE_DIR)

    if account_df.empty or meter_df.empty:
        print("No account rows and/or no meter rows extracted -- skipping both CSV writes.")
        print("Aborting to avoid uploading older CSVs; exiting with non-zero status.")
        sys.exit(1)

    account_df, updates_df = reconcile_long_interval_bills(account_df)
    if not updates_df.empty:
        print("Updates were made to overlapping periods:")
        print(updates_df)

    # config.OUTPUT_CSV only supplies the output folder; both files have fixed
    # names and are overwritten on every run.
    output_dir = Path(config.OUTPUT_CSV).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    account_path = output_dir / ACCOUNT_CSV_NAME
    meter_path = output_dir / METER_CSV_NAME
    account_df.to_csv(account_path, index=False)
    meter_df.to_csv(meter_path, index=False)

    print(f"Processing complete. CSVs saved:")
    print(f"  {account_path}  ({len(account_df)} rows)")
    print(f"  {meter_path}  ({len(meter_df)} rows)")
    if problems:
        print(f"\n{len(problems)} item(s) to review before uploading:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("All bills passed the checks.")