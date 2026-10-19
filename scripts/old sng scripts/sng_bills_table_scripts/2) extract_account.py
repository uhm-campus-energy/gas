"""
2) Extract gas bill data into a CSV to then be uploaded to gas.sng_bills table.

The PDF parsing logic follows the original SNG gas bill extractor. Paths and
output columns come from config.py so this script can run on any machine.

therms is the bill's own printed account-wide total (the "Consumption
Charge" line), not a sum of individual meters -- one row per account per
bill, not one row per meter.
"""

import glob
import re
import sys
from pathlib import Path
import pandas as pd
from pypdf import PdfReader
import tempfile

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

# Meter table row -- only used here to read the billing period's reading
# dates (every meter on an account is read on the same two days).
ROW_REGEX_PAGE1 = (
    r"\b\d{4,7}\s+"  # meter ID: 4-7 digits (some are 5, e.g. 57729)
    r"(\d{2}/\d{2}/\d{4})\s+"
    r"\d+\s+"
    r"(\d{2}/\d{2}/\d{4})\s+"
    r"\d+\s+"
    r"\d+\s+"
    r"[0-9.]+\s+"
     r"([\d,]+\.\d{2})"
)

# Used to check if there's overlapping billing dates
LONG_INTERVAL_DAYS = 40


def extract_pdf(pdf_path):
    """Extract one row per account from a gas bill PDF."""
    account = None
    billed_amount = None
    total_therms = None
    date_from = None
    date_to = None
    meter_therms = []

    with open(pdf_path, "rb") as pdf_file:
        reader = PdfReader(pdf_file)

        for page in reader.pages:
            text = page.extract_text() or ""

            if account is None:
                account_match = re.search(ACCOUNT_REGEX, text)
                if account_match:
                    account = account_match.group(1)

            if billed_amount is None:
                billed_match = re.search(BILLED_REGEX, text)
                if billed_match:
                    billed_amount = float(billed_match.group(1).replace(",", ""))

            if total_therms is None:
                therms_match = re.search(TOTAL_THERMS_REGEX, text)
                if therms_match == None:
                    therms_match = re.search(r"Consumption\s+([\d,]+\.\d{2})", text)
                if therms_match:
                    total_therms = float(therms_match.group(1).replace(",", ""))
                    
            for row_match in re.finditer(ROW_REGEX_PAGE1, text):
                if date_from is None:
                    date_from = row_match.group(1)
                    date_to = row_match.group(2)
                meter_therms.append(float(row_match.group(3).replace(",", "")))

    # Only fall back to the meter table once ALL pages have been read. The
    # consumption charge line is usually on page 2, so checking after page 1
    # would wrongly sum just the page 1 meters.
    note = ""
    if total_therms is None and meter_therms:
        total_therms = round(sum(meter_therms), 2)
        note = "no consumption charge line; therms from meter table"
        if total_therms == 0:
            print(f"[INFO] Account {account}: no consumption charge, meter table confirms 0.00 therms")
        else:
            print(
                f"[WARNING] Account {account}: no consumption charge line but "
                f"meter table sums to {total_therms} therms -- please double-check"
            )

    if account is None or billed_amount is None or total_therms is None or date_from is None:
        return []

    if pd.to_datetime(date_to) - pd.to_datetime(date_from) > pd.Timedelta(days=LONG_INTERVAL_DAYS):
        print(f"Long billing interval detected for account {account}: {date_from} to {date_to}")

    return [{
        "account": account,
        "date_from": pd.to_datetime(date_from).strftime("%Y-%m-%d"),
        "date_to": pd.to_datetime(date_to).strftime("%Y-%m-%d"),
        "therms": total_therms,
        "therms_adjusted": total_therms,
        "billed_$": billed_amount,
        "note": note,
    }]

def get_account_and_date_to(pdf_path):
    """
    Read just the account number and billing-period end date from a PDF --
    for files sitting in duplicate_bills/ that are still under their
    original (non-canonical) filename. Reuses the same regexes as
    extract_pdf(), just without needing the therms/billed amount.

    Returns (account, date_to) as (str, datetime.date), or (None, None)
    if either couldn't be read.
    """
    account = None
    date_to = None

    with open(pdf_path, "rb") as pdf_file:
        reader = PdfReader(pdf_file)
        for page in reader.pages:
            text = page.extract_text() or ""

            if account is None:
                match = re.search(ACCOUNT_REGEX, text)
                if match:
                    account = match.group(1)

            if date_to is None:
                match = re.search(ROW_REGEX_PAGE1, text)
                if match:
                    date_to = pd.to_datetime(match.group(2)).date()

    return account, date_to


def extract_all_bills(input_folder):
    """Extract all PDFs under input_folder except files in duplicates."""
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

    print(f"Found {len(all_pdfs)} PDF files total, skipping {skipped_duplicates} in duplicates")
    all_rows = []

    for pdf_path in pdf_files:
        try:
            all_rows.extend(extract_pdf(pdf_path))
        except Exception as error:
            print(f"Error reading {pdf_path}: {error}")

    print(f"Extracted {len(all_rows)} account rows from {len(pdf_files)} PDFs")
    return pd.DataFrame(all_rows, columns=config.COLUMNS)


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

    df: the DataFrame from extract_all_bills(), one row per account per
        newly-parsed PDF (columns: account, date_from, date_to, therms,
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
    print("Starting gas bill extraction from PDFs...")
    dataframe = extract_all_bills(config.SOURCE_DIR)
    dataframe, updates_df = reconcile_long_interval_bills(dataframe)
    if not updates_df.empty:
        print("Updates were made to overlapping periods:")
        print(updates_df)

    if dataframe.empty:
        print("No PDFs to extract -- skipping CSV write.")
        print("Aborting to avoid uploading an older CSV; exiting with non-zero status.")
        sys.exit(1)
    else:
        # config.OUTPUT_CSV only supplies the output folder; the file is always
        # named gas_acc_extracted.csv and is overwritten on every run.
        output_path = Path(config.OUTPUT_CSV).parent / "gas_acc_extracted.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        dataframe.to_csv(output_path, index=False)

        print(f"Processing complete. CSV saved to {output_path}")