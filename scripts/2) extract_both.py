"""
2) Extract gas bill data from the PDFs into TWO CSVs for the invoice-based
tables, reading each PDF once:

    gas_bill_summary_extracted.csv
        one row per invoice            -> gas_bill_summary
    gas_bill_meter_readings_extracted.csv
        one row per meter per invoice  -> gas_bill_meter_readings

Both files go in the output folder (from config.py) and are overwritten on
every run. updated_at is left blank here; the upload script fills it in.

Summary row: everything comes from the "ACCOUNT SUMMARY" block printed at the
top of the bill, plus:
  total_therms  the bill's own printed account-wide total (the "Consumption
                Charge" line); if there isn't one, the meter table's sum
  meter_count   number of different meters on the bill
  pdf_filename  ACCOUNT_YYMMDD_inv<INVOICE>.pdf (YYMMDD = current bill date)
Money amounts marked "CR" (credit) are stored as negative numbers, except
payments, which the bill always prints as "CR" and are stored as positive.

Meter row: therms is the value PRINTED on the bill for each meter. usage is
reading_to - reading_from (if the dial rolled over, the dial size is chosen so
that usage x factor matches the printed therms). Two checks run on every bill:
  1. Each meter: usage x factor is close to printed therms.
  2. Each bill: the meter therms add up to the bill's "Consumption Charge" therms.
Problems are printed as warnings and listed at the end. Rows are still written
so you can look at them before anything is uploaded.

Run order:  2) this script  ->  3) upload_bill_tables.py  ->  4) archive_bill_tables.py
"""

import glob
import re
import sys
from datetime import datetime
from pathlib import Path
import pandas as pd
from pypdf import PdfReader

# Needed in order to import config
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config


# ── What to look for on the bill ─────────────────────────────────────────

# ACCOUNT SUMMARY block (printed at the top of every page). "^" anchors each
# label to the start of its line so the payment-stub text further down
# ("Current Charge", "Previous Bal - Pymnts/Adj") can't be picked up by mistake.
DATE = r"(\d{2}/\d{2}/\d{4})"
MONEY = r"\$([\d,]+\.\d{2})(\s*CR)?"      # group 1 = amount, group 2 = "CR" if a credit

ACCOUNT_REGEX = re.compile(r"Account Number\s+(\d+)")
INVOICE_REGEX = re.compile(r"^Invoice Number\s+(\d+)", re.MULTILINE)
LAST_BILL_REGEX = re.compile(rf"^Last Bill Date\s+{DATE}", re.MULTILINE)
CURRENT_BILL_REGEX = re.compile(rf"^Current Bill Date\s+{DATE}", re.MULTILINE)
DUE_DATE_REGEX = re.compile(rf"^Due Date\s+{DATE}", re.MULTILINE)
PREVIOUS_BALANCE_REGEX = re.compile(rf"^Previous Balance\s+{MONEY}", re.MULTILINE)
PAYMENTS_REGEX = re.compile(rf"^Payments\s+{MONEY}", re.MULTILINE)
CURRENT_CHARGES_REGEX = re.compile(rf"^Current Charges\s*{MONEY}", re.MULTILINE)
TOTAL_DUE_REGEX = re.compile(rf"^Total Amount Due\s+{MONEY}", re.MULTILINE)

# Account-wide total therms, printed once in the "Current Charges" breakdown
# table (page 2) -- this is the WHOLE ACCOUNT's total, not one meter's.
TOTAL_THERMS_REGEX = r"Consumption Charge\s+([\d,]+\.\d{2})"
TOTAL_THERMS_FALLBACK_REGEX = r"Consumption\s+([\d,]+\.\d{2})"

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

SUMMARY_COLUMNS = [
    "account_number",
    "invoice_number",
    "last_bill_date",
    "current_bill_date",
    "due_date",
    "previous_balance",
    "payments",
    "current_charges",
    "total_amount_due",
    "total_therms",
    "meter_count",
    "pdf_filename",
    "updated_at",
]

METER_COLUMNS = [
    "account_number",
    "invoice_number",
    "meter_id",
    "date_from",
    "reading_from",
    "date_to",
    "reading_to",
    "usage",
    "days",
    "factor",
    "therms",
    "pdf_filename",
    "updated_at",
]

SUMMARY_CSV_NAME = "gas_bill_summary_extracted.csv"
METERS_CSV_NAME = "gas_bill_meter_readings_extracted.csv"

# Dial sizes to try when a meter reading goes down (4, 5 or 6-digit dials)
DIAL_SIZES = (10**4, 10**5, 10**6)

# The bill prints factors rounded to 5 decimals, so (usage x factor) can be off
# the printed therms by a few hundredths. Allow that much before warning.
METER_TOLERANCE = 0.5
BILL_TOLERANCE = 0.5

# Used to flag unusually long billing periods
LONG_INTERVAL_DAYS = 40


# ── Small helpers ────────────────────────────────────────────────────────

def parse_date(text):
    """'07/31/2026' -> datetime.date. None stays None."""
    return datetime.strptime(text, "%m/%d/%Y").date() if text else None


def parse_money(match, always_positive=False):
    """
    Turn a MONEY regex match into a float. A trailing 'CR' means credit, so
    the amount is negative -- unless always_positive (payments are always
    printed as 'CR' but are stored as positive amounts). None stays None.
    """
    if match is None:
        return None
    amount = float(match.group(1).replace(",", ""))
    if match.group(2) and not always_positive:
        amount = -amount
    return amount


def make_pdf_filename(account, current_bill_date, invoice):
    """ACCOUNT_YYMMDD_inv<INVOICE>.pdf, e.g. 2014354_260731_inv3310107.pdf"""
    return f"{account}_{current_bill_date.strftime('%y%m%d')}_inv{invoice}.pdf"


def meter_usage(reading_from, reading_to, factor, therms):
    """
    reading_to - reading_from. If the reading went DOWN the dial rolled over,
    so add the dial size back. The dial size (4, 5 or 6 digits) is whichever
    one makes usage x factor closest to the therms printed on the bill.
    """
    usage = reading_to - reading_from
    if usage >= 0:
        return usage

    best_usage, best_error = None, None
    for dial in DIAL_SIZES:
        if dial <= reading_from:
            continue  # a reading can't be bigger than the dial
        candidate = usage + dial
        error = abs(candidate * factor - therms)
        if best_error is None or error < best_error:
            best_usage, best_error = candidate, error
    return best_usage if best_usage is not None else usage + DIAL_SIZES[-1]


# ── Reading one PDF ──────────────────────────────────────────────────────

def read_bill(pdf_path):
    """
    Read one PDF once. Returns (info, raw_rows).
      info     dict: account, invoice, last_bill_date, current_bill_date,
               due_date (datetime.date), previous_balance, payments,
               current_charges, total_amount_due, total_therms (floats).
               Anything not found on the bill is None.
      raw_rows list of meter-row regex groups (see METER_ROW_REGEX).
    The archive script also uses this to read a bill's keys.
    """
    info = {
        "account": None, "invoice": None,
        "last_bill_date": None, "current_bill_date": None, "due_date": None,
        "previous_balance": None, "payments": None, "current_charges": None,
        "total_amount_due": None, "total_therms": None,
    }
    raw_rows = []

    with open(pdf_path, "rb") as pdf_file:
        reader = PdfReader(pdf_file)

        for page in reader.pages:
            text = page.extract_text() or ""

            if info["account"] is None:
                match = ACCOUNT_REGEX.search(text)
                if match:
                    info["account"] = match.group(1)

            if info["invoice"] is None:
                match = INVOICE_REGEX.search(text)
                if match:
                    info["invoice"] = match.group(1)

            for key, regex in (("last_bill_date", LAST_BILL_REGEX),
                               ("current_bill_date", CURRENT_BILL_REGEX),
                               ("due_date", DUE_DATE_REGEX)):
                if info[key] is None:
                    match = regex.search(text)
                    if match:
                        info[key] = parse_date(match.group(1))

            for key, regex, positive in (("previous_balance", PREVIOUS_BALANCE_REGEX, False),
                                         ("payments", PAYMENTS_REGEX, True),
                                         ("current_charges", CURRENT_CHARGES_REGEX, False),
                                         ("total_amount_due", TOTAL_DUE_REGEX, False)):
                if info[key] is None:
                    info[key] = parse_money(regex.search(text), always_positive=positive)

            if info["total_therms"] is None:
                match = re.search(TOTAL_THERMS_REGEX, text)
                if match is None:
                    match = re.search(TOTAL_THERMS_FALLBACK_REGEX, text)
                if match:
                    info["total_therms"] = float(match.group(1).replace(",", ""))

            # Meter rows can be on any page (large accounts spill onto page 2)
            for row_match in re.finditer(METER_ROW_REGEX, text):
                raw_rows.append(row_match.groups())

    return info, raw_rows


# ── Building the rows ────────────────────────────────────────────────────

def build_meter_records(info, pdf_filename, raw_rows):
    """Turn raw regex groups into meter row dicts. Returns (records, meter_issues)."""
    records = []
    meter_issues = []

    for meter_id, date_from, read_from, date_to, read_to, days, factor, therms in raw_rows:
        reading_from = int(read_from)
        reading_to = int(read_to)
        factor = float(factor)
        therms = float(therms.replace(",", ""))
        usage = meter_usage(reading_from, reading_to, factor, therms)

        expected = usage * factor
        if abs(expected - therms) > max(METER_TOLERANCE, 0.001 * therms):
            meter_issues.append(
                f"meter {meter_id}: usage {usage} x factor {factor} = {expected:.2f}, "
                f"but bill prints {therms:.2f}"
            )

        records.append({
            "account_number": info["account"],
            "invoice_number": info["invoice"],
            "meter_id": meter_id,
            "date_from": parse_date(date_from).strftime("%Y-%m-%d"),
            "reading_from": reading_from,
            "date_to": parse_date(date_to).strftime("%Y-%m-%d"),
            "reading_to": reading_to,
            "usage": usage,
            "days": int(days),
            "factor": factor,
            "therms": therms,
            "pdf_filename": pdf_filename,
            "updated_at": None,  # blank here; the upload script sets it
        })

    return records, meter_issues


def build_summary_row(info, pdf_filename, raw_rows, meter_sum):
    """
    Build the one gas_bill_summary row for a bill. Returns (row, notes).
    notes is a list of messages to print. Fields the bill didn't have are
    left blank; the caller reports them.
    """
    notes = []

    total_therms = info["total_therms"]
    if total_therms is None:
        # No "Consumption Charge" line: use the meter table instead.
        total_therms = meter_sum
        if total_therms == 0:
            notes.append("[INFO] no consumption charge, meter table confirms 0.00 therms")
        else:
            notes.append(
                f"[WARNING] no consumption charge line but meter table sums to "
                f"{total_therms} therms -- please double-check"
            )

    last_bill, current_bill = info["last_bill_date"], info["current_bill_date"]
    if last_bill and (current_bill - last_bill).days > LONG_INTERVAL_DAYS:
        notes.append(
            f"Long billing interval detected for account {info['account']}: {last_bill} to {current_bill}"
        )

    fmt = lambda d: d.strftime("%Y-%m-%d") if d else None
    return {
        "account_number": info["account"],
        "invoice_number": info["invoice"],
        "last_bill_date": fmt(last_bill),
        "current_bill_date": fmt(current_bill),
        "due_date": fmt(info["due_date"]),
        "previous_balance": info["previous_balance"],
        "payments": info["payments"],
        "current_charges": info["current_charges"],
        "total_amount_due": info["total_amount_due"],
        "total_therms": total_therms,
        "meter_count": len({row[0] for row in raw_rows}),
        "pdf_filename": pdf_filename,
        "updated_at": None,  # blank here; the upload script sets it
    }, notes


# ── Running over every PDF ───────────────────────────────────────────────

def extract_all_bills(input_folder):
    """
    Extract every PDF under input_folder except files in duplicates.
    Returns (summary_df, meters_df, problems).
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

    summary_rows = []
    meter_rows = []
    problems = []
    seen_invoices = {}  # (account, invoice) -> first PDF name, to catch the same bill twice

    for number, pdf_path in enumerate(pdf_files, start=1):
        label = f"[{number}/{total}] {pdf_path.name}"

        try:
            info, raw_rows = read_bill(pdf_path)
        except Exception as error:
            print(f"{label} -> ERROR: {error}")
            problems.append(f"{pdf_path.name}: could not be read ({error})")
            continue

        if info["account"] is None or not raw_rows:
            print(f"{label} -> WARNING: no account/meter rows found, skipped")
            problems.append(f"{pdf_path.name}: no account or meter rows found")
            continue

        missing_keys = [name for name, value in (("invoice number", info["invoice"]),
                                                 ("current bill date", info["current_bill_date"]))
                        if value is None]
        if missing_keys:
            print(f"{label} -> WARNING: no {' / '.join(missing_keys)} found, skipped")
            problems.append(f"{pdf_path.name}: no {' / '.join(missing_keys)} found")
            continue

        key = (info["account"], info["invoice"])
        if key in seen_invoices:
            print(f"{label} -> WARNING: same invoice as {seen_invoices[key]}, skipped")
            problems.append(f"{pdf_path.name}: invoice {info['invoice']} was already read from {seen_invoices[key]}")
            continue
        seen_invoices[key] = pdf_path.name

        pdf_filename = make_pdf_filename(info["account"], info["current_bill_date"], info["invoice"])

        # Meter rows + checks
        records, meter_issues = build_meter_records(info, pdf_filename, raw_rows)
        meter_sum = round(sum(r["therms"] for r in records), 2)
        issues = list(meter_issues)

        if info["total_therms"] is None:
            summary = (f"invoice {info['invoice']}, {len(records)} meters, {meter_sum:,.2f} therms "
                       f"(no consumption charge line to compare against)")
        elif abs(meter_sum - info["total_therms"]) > BILL_TOLERANCE:
            issues.append(
                f"meters add up to {meter_sum:,.2f} therms but the bill total is {info['total_therms']:,.2f}"
            )
            summary = f"invoice {info['invoice']}, {len(records)} meters, WARNING: total mismatch"
        else:
            summary = (f"invoice {info['invoice']}, {len(records)} meters, "
                       f"{meter_sum:,.2f} therms, matches bill total")
        print(f"{label} -> {summary}")

        # Summary row
        summary_row, summary_notes = build_summary_row(info, pdf_filename, raw_rows, meter_sum)
        blank = [name for name in ("last_bill_date", "due_date", "previous_balance", "payments",
                                   "current_charges", "total_amount_due")
                 if summary_row[name] is None]
        if blank:
            issues.append(f"not found on the bill, left blank: {', '.join(blank)}")

        for note in summary_notes:
            print(f"      {note}")
        for issue in issues:
            print(f"      [WARNING] {issue}")
            problems.append(f"{pdf_path.name}: {issue}")

        summary_rows.append(summary_row)
        meter_rows.extend(records)

    print(f"Extracted {len(summary_rows)} invoices and {len(meter_rows)} meter rows from {total} PDFs")
    return (pd.DataFrame(summary_rows, columns=SUMMARY_COLUMNS),
            pd.DataFrame(meter_rows, columns=METER_COLUMNS),
            problems)


if __name__ == "__main__":
    print("Starting gas bill extraction from PDFs (invoice summary + meter readings CSVs)...")
    summary_df, meters_df, problems = extract_all_bills(config.SOURCE_DIR)

    if summary_df.empty or meters_df.empty:
        print("No invoices and/or no meter rows extracted -- skipping both CSV writes.")
        print("Aborting to avoid uploading older CSVs; exiting with non-zero status.")
        sys.exit(1)

    # config.OUTPUT_CSV only supplies the output folder; both files have fixed
    # names and are overwritten on every run.
    output_dir = Path(config.OUTPUT_CSV).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / SUMMARY_CSV_NAME
    meters_path = output_dir / METERS_CSV_NAME
    summary_df.to_csv(summary_path, index=False)
    meters_df.to_csv(meters_path, index=False)

    print("Processing complete. CSVs saved:")
    print(f"  {summary_path}  ({len(summary_df)} rows)")
    print(f"  {meters_path}  ({len(meters_df)} rows)")
    if problems:
        print(f"\n{len(problems)} item(s) to review before uploading:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("All bills passed the checks.")