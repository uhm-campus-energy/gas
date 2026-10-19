"""
4) Archive gas bills, but ONLY bills confirmed in BOTH invoice tables:

    - gas_bill_summary         (invoice level: account_number + invoice_number)
    - gas_bill_meter_readings  (meter level: every meter on the bill, matched on
                                invoice_number + meter_id + that meter's date_to)

Both are filled by "3) upload_bill_tables.py".

Only runs where psql can reach the database (the server). For each PDF in
input_bills/ this script reads the account, invoice number and meter rows
straight from the PDF (same parsing as "2) extract_bill_tables.py"), asks the
database whether the bill is in both tables, and moves the PDF into
bill_archive/FY<YY>/<account>/ -- renamed to ACCOUNT_YYYY_MM_DD_gas_bill.pdf
(date = the bill's current bill date) -- only if it is. A bill missing from
either table stays where it is, so a fix + re-run can't lose its database rows.
If that name is already in the archive, the invoice number inside the existing
file is compared: the same invoice means a duplicate, so the PDF is left in
place; a different invoice gets a _1, _2, ... suffix instead of overwriting.

LPG bills are skipped here (they have no meter rows); the LPG scripts archive them.

NOTE: this MOVES the PDFs out of input_bills/, the same as the archive script
for the gas.sng_bills / gas.sng pipeline. Once one archive script has moved a
bill, the other one can no longer find it.

Run order:  2) extract_bill_tables.py  ->  3) upload_bill_tables.py  ->  4) this script

Paths come from config.py, same as the other scripts.
"""

import contextlib
import csv
import importlib.util
import io
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path so we can import `config` and `gas_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import extract_query_to_csv, get_fiscal_year, LPG_ACCOUNT
import gbt_2_extract as bill_extract

DATABASE = "uhm2023"
SUMMARY_TABLE = "gas.gas_bill_summary"
METERS_TABLE = "gas.gas_bill_meter_readings"


# ── Reading the PDF and checking the database ────────────────────────────

def read_bill_keys(pdf_path):
    """
    Returns a dict for one PDF:
      account, invoice (str), current_bill_date (datetime.date),
      pdf_filename (str, as stored in the database), and
      meter_keys (set of (meter_id, date_to) pairs; meter_id is text, date_to a date).
    Returns None if the PDF can't be read as a metered gas bill.
    """
    info, raw_rows = bill_extract.read_bill(pdf_path)
    if info["account"] is None or info["invoice"] is None \
            or info["current_bill_date"] is None or not raw_rows:
        return None

    # raw row groups: meter_id, date_from, read1, date_to, read2, days, factor, therms
    meter_keys = {(row[0], bill_extract.parse_date(row[3])) for row in raw_rows}
    return {
        "account": info["account"],
        "invoice": info["invoice"],
        "current_bill_date": info["current_bill_date"],
        "pdf_filename": bill_extract.make_pdf_filename(
            info["account"], info["current_bill_date"], info["invoice"]
        ),
        "meter_keys": meter_keys,
    }


def query_rows(query, label):
    """
    Run a query through psql into a temp CSV and return its data rows (a list
    of lists, header removed). Returns None if the query itself failed.
    """
    csv_path = Path(tempfile.gettempdir()) / f"gas_archive_{label}.csv"

    # extract_query_to_csv prints an [OK] line for every query; keep that
    # quiet, but show everything it printed if the query failed.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        pulled = extract_query_to_csv(DATABASE, query, csv_path)
    if not pulled:
        print(buffer.getvalue().rstrip())

    rows = None
    if pulled and csv_path.exists():
        with open(csv_path, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # header row
            rows = list(reader)
    if csv_path.exists():
        csv_path.unlink()
    return rows


def confirm_in_database(pdf_path):
    """
    Check one PDF against both tables.

    Returns (status, bill, detail). bill is the dict from read_bill_keys (None
    if unreadable). status is one of:
      "ok"             -- in gas_bill_summary AND every meter is in gas_bill_meter_readings
      "unreadable"     -- couldn't read account/invoice/meters from the PDF
      "db_error"       -- a database query failed
      "no_bills_row"   -- no gas_bill_summary row for this account + invoice
      "meters_missing" -- one or more meters not in gas_bill_meter_readings
    """
    bill = read_bill_keys(pdf_path)
    if bill is None:
        return "unreadable", None, "couldn't read account/invoice/meter rows from the PDF"

    account, invoice = bill["account"], bill["invoice"]

    # Invoice level: gas_bill_summary
    bills_rows = query_rows(
        f"SELECT 1 AS found FROM {SUMMARY_TABLE} "
        f"WHERE account_number = '{account}' AND invoice_number = '{invoice}' LIMIT 1",
        f"summary_{account}_{invoice}",
    )
    if bills_rows is None:
        return "db_error", bill, f"query against {SUMMARY_TABLE} failed"
    if not bills_rows:
        return "no_bills_row", bill, (
            f"no row in {SUMMARY_TABLE} for account {account}, invoice {invoice}"
        )

    # Meter level: gas_bill_meter_readings (every meter on the bill must be
    # there, matched on that meter's own date_to)
    meter_keys = bill["meter_keys"]
    pair_list = ", ".join(
        f"('{meter}', '{d.strftime('%Y-%m-%d')}')" for meter, d in sorted(meter_keys)
    )
    meter_rows = query_rows(
        f"SELECT meter_id, date_to FROM {METERS_TABLE} "
        f"WHERE account_number = '{account}' AND invoice_number = '{invoice}' "
        f"AND (meter_id, date_to) IN ({pair_list})",
        f"meters_{account}_{invoice}",
    )
    if meter_rows is None:
        return "db_error", bill, f"query against {METERS_TABLE} failed"

    found = {(row[0].strip(), datetime.strptime(row[1][:10], "%Y-%m-%d").date())
             for row in meter_rows}
    missing = meter_keys - found
    if missing:
        shown = ", ".join(f"{meter} ({d})" for meter, d in sorted(missing)[:5])
        more = f", +{len(missing) - 5} more" if len(missing) > 5 else ""
        return "meters_missing", bill, (
            f"{len(missing)} of {len(meter_keys)} meters not in {METERS_TABLE}: {shown}{more}"
        )

    return "ok", bill, f"invoice {invoice}, {len(meter_keys)} meters confirmed"


# ── Moving the PDF into the archive ──────────────────────────────────────

def archive_name(bill):
    """ACCOUNT_YYYY_MM_DD_gas_bill.pdf, where the date is the current bill date."""
    return f"{bill['account']}_{bill['current_bill_date'].strftime('%Y_%m_%d')}_{config.FILENAME_SUFFIX}.pdf"


def is_same_invoice(existing_pdf, invoice):
    """True if an already-archived PDF is the same invoice (or can't be read, to be safe)."""
    try:
        info, _rows = bill_extract.read_bill(existing_pdf)
    except Exception:
        return True
    return info["invoice"] == invoice


def archive_pdf(pdf_path, bill):
    """
    Move one confirmed bill into bill_archive/FY<YY>/<account>/, renamed to
    ACCOUNT_YYYY_MM_DD_gas_bill.pdf (date = current bill date). The fiscal year
    comes from the same date, same Jul-Dec rule as the rest of the pipeline.

    If that name is already taken in the archive:
      - same invoice  -> it's a duplicate; the PDF is left in place
      - different invoice (e.g. a second bill with the same date) -> saved with
        a _1, _2, ... suffix instead

    Returns the new path of the archived file, or None if it was left in place.
    """
    bill_date = bill["current_bill_date"]
    fy_folder = get_fiscal_year(bill_date.year, bill_date.month)
    target_dir = Path(config.ARCHIVE_DIR) / fy_folder / bill["account"]

    name = archive_name(bill)
    destination = target_dir / name
    counter = 1
    while destination.exists():
        if is_same_invoice(destination, bill["invoice"]):
            return None
        destination = target_dir / f"{Path(name).stem}_{counter}.pdf"
        counter += 1

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf_path), str(destination))
    return destination


# ── Running a pass over a list of PDFs ───────────────────────────────────

def archive_confirmed_bills(pdf_files):
    """Check each PDF against both tables and archive the confirmed ones."""
    counts = {"ok": 0, "unreadable": 0, "db_error": 0, "no_bills_row": 0, "meters_missing": 0}
    archived = 0
    already_archived = 0
    total = len(pdf_files)

    for number, pdf_path in enumerate(pdf_files, start=1):
        label = f"[{number}/{total}] {pdf_path.name}"
        try:
            status, bill, detail = confirm_in_database(pdf_path)
        except Exception as error:
            print(f"{label} -> ERROR: {error}")
            counts["unreadable"] += 1
            continue

        counts[status] += 1
        if status != "ok":
            print(f"{label} -> left in place: {detail}")
            continue

        destination = archive_pdf(pdf_path, bill)
        if destination:
            shown = destination.relative_to(Path(config.ARCHIVE_DIR)).as_posix()
            print(f"{label} -> archived to {shown} ({detail})")
            archived += 1
        else:
            print(f"{label} -> left in place: invoice {bill['invoice']} is already in the archive")
            already_archived += 1

    print(f"\nArchived: {archived} of {total}")
    print(f"  Left in place, not in {SUMMARY_TABLE}:        {counts['no_bills_row']}")
    print(f"  Left in place, meters missing from {METERS_TABLE}: {counts['meters_missing']}")
    print(f"  Left in place, couldn't read PDF:         {counts['unreadable']}")
    print(f"  Left in place, database check failed:     {counts['db_error']}")
    print(f"  Left in place, already in the archive:    {already_archived}")
    return archived


def remove_empty_subfolders(source_dir):
    """Delete empty folders left behind in input_bills/ (never duplicate_bills/)."""
    duplicate_dir = str(Path(config.DUPLICATE_DIR).resolve())
    removed = 0
    for root, dirs, _ in os.walk(source_dir, topdown=False):
        for name in dirs:
            dir_path = Path(root) / name
            if str(dir_path.resolve()).startswith(duplicate_dir):
                continue
            if not any(dir_path.iterdir()):
                dir_path.rmdir()
                removed += 1
    print(f"Empty subfolders removed: {removed}")


def find_input_pdfs():
    """PDFs under input_bills/ excluding duplicate_bills/, combined files and LPG bills."""
    duplicate_dir = Path(config.DUPLICATE_DIR).resolve()
    pdf_files = []
    lpg_skipped = 0
    for pdf_path in Path(config.SOURCE_DIR).rglob("*.pdf"):
        resolved = pdf_path.resolve()
        if resolved == duplicate_dir or duplicate_dir in resolved.parents:
            continue
        if pdf_path.name.startswith("combined_"):
            continue
        if pdf_path.name.startswith(LPG_ACCOUNT) or pdf_path.parent.name == LPG_ACCOUNT:
            lpg_skipped += 1
            continue
        pdf_files.append(pdf_path)
    return sorted(pdf_files), lpg_skipped


if __name__ == "__main__":
    print(f"Starting bill archiving (a bill needs rows in {SUMMARY_TABLE} AND {METERS_TABLE})...")

    input_pdfs, lpg_skipped = find_input_pdfs()
    if lpg_skipped:
        print(f"Skipping {lpg_skipped} LPG bill(s) -- the LPG scripts archive those.")
    if input_pdfs:
        archive_confirmed_bills(input_pdfs)
        remove_empty_subfolders(config.SOURCE_DIR)
    else:
        print("No PDF files found in input_bills/.")

    print("\nDone.")