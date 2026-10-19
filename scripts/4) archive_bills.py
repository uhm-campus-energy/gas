"""
6) Archive gas bills, but ONLY bills confirmed in BOTH tables:

    - gas.sng_bills  (account level: account + date_to)    -- uploaded by 5)
    - gas.sng        (meter level: every meter on the bill
                      at that date_to)                      -- uploaded by 4)

Only runs where psql can reach the database (the server). For each PDF this
script reads the account, the billing-period end date and the meter IDs
straight from the PDF (same parsing as "3) extract_meter.py"), asks the
database whether the bill is there, and moves the PDF into
bill_archive/FY<YY>/<account>/ only if it is. A bill missing from either
table stays where it is, so a fix + re-run can't lose its database rows.

Two passes:
  1. input_bills/        -- the bills just processed by 2) through 5)
  2. duplicate_bills/    -- backfill sweep: bills whose data is already in the
                            database (e.g. uploaded before archiving existed)
                            are renamed to canonical form and archived too

LPG bills are skipped here (they have no meter rows); the LPG scripts archive them.

Run order:  2) -> 3) -> 4) and 5) -> 6) this script

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
from datetime import date, datetime
from pathlib import Path

# Ensure project root is on sys.path so we can import `config` and `gas_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import extract_query_to_csv, get_fiscal_year, LPG_ACCOUNT

# "3) extract_meter.py" isn't a valid module name to `import` directly
# (space + parenthesis), so load it by path to reuse its PDF reader.
_extract_spec = importlib.util.spec_from_file_location(
    "gas_meter_extract", SCRIPT_DIR / "3) extract_meter.py"
)
gas_meter_extract = importlib.util.module_from_spec(_extract_spec)
_extract_spec.loader.exec_module(gas_meter_extract)

DATABASE = "uhm2023"
BILLS_TABLE = "gas.sng_bills"
METERS_TABLE = "gas.sng"


# ── Reading the PDF and checking the database ────────────────────────────

def read_bill_keys(pdf_path):
    """
    Returns (account, date_to, meter_keys) for one PDF: the account number
    (str), the bill's billing-period end date (datetime.date; the first meter's,
    same as 2) extract_account.py uses for gas.sng_bills) and the set of
    (meter_id, date_to) pairs on the bill. Meters on one bill don't always share
    a read date, so each meter is checked against its OWN date_to.
    Returns (None, None, set()) if it can't be read.
    """
    account, _bill_total, raw_rows = gas_meter_extract.read_bill(pdf_path)
    if account is None or not raw_rows:
        return None, None, set()
    # raw row groups: meter_id, date_from, read1, date_to, read2, days, factor, therms
    date_to = datetime.strptime(raw_rows[0][3], "%m/%d/%Y").date()
    # Keep meter IDs as text: gas.sng.meter_id is varchar, and this matches
    # exactly what the extract/upload scripts wrote.
    meter_keys = {
        (row[0], datetime.strptime(row[3], "%m/%d/%Y").date())
        for row in raw_rows
    }
    return account, date_to, meter_keys


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

    Returns (status, account, date_to, detail). status is one of:
      "ok"             -- in gas.sng_bills AND every meter is in gas.sng
      "unreadable"     -- couldn't read account/meters from the PDF
      "db_error"       -- a database query failed
      "no_bills_row"   -- no gas.sng_bills row for this account + date_to
      "meters_missing" -- one or more meters not in gas.sng at date_to
    """
    account, date_to, meter_keys = read_bill_keys(pdf_path)
    if account is None:
        return "unreadable", None, None, "couldn't read account/meter rows from the PDF"

    date_str = date_to.strftime("%Y-%m-%d")

    # Account level: gas.sng_bills
    bills_rows = query_rows(
        f"SELECT 1 AS found FROM {BILLS_TABLE} "
        f"WHERE account = '{account}' AND date_to = '{date_str}' LIMIT 1",
        f"bills_{account}_{date_str}",
    )
    if bills_rows is None:
        return "db_error", account, date_to, f"query against {BILLS_TABLE} failed"
    if not bills_rows:
        return "no_bills_row", account, date_to, (
            f"no row in {BILLS_TABLE} for account {account}, date_to {date_str}"
        )

    # Meter level: gas.sng (every meter on the bill must be there, matched on
    # meter_id + that meter's own date_to)
    pair_list = ", ".join(
        f"('{meter}', '{d.strftime('%Y-%m-%d')}')" for meter, d in sorted(meter_keys)
    )
    meter_rows = query_rows(
        f"SELECT meter_id, date_to FROM {METERS_TABLE} "
        f"WHERE (meter_id, date_to) IN ({pair_list})",
        f"meters_{account}_{date_str}",
    )
    if meter_rows is None:
        return "db_error", account, date_to, f"query against {METERS_TABLE} failed"

    found = {(row[0].strip(), datetime.strptime(row[1][:10], "%Y-%m-%d").date()) for row in meter_rows}
    missing = meter_keys - found
    if missing:
        shown = ", ".join(f"{meter} ({d})" for meter, d in sorted(missing)[:5])
        more = f", +{len(missing) - 5} more" if len(missing) > 5 else ""
        return "meters_missing", account, date_to, (
            f"{len(missing)} of {len(meter_keys)} meters not in {METERS_TABLE}: {shown}{more}"
        )

    return "ok", account, date_to, f"{len(meter_keys)} meters confirmed"


# ── Moving the PDF into the archive ──────────────────────────────────────

def archive_pdf(pdf_path, account, date_to, rename_to_canonical):
    """
    Move one confirmed bill into bill_archive/FY<YY>/<account>/. The fiscal
    year comes from the date in the canonical filename, same as the rest of
    the pipeline. If the file isn't canonically named, it is only archived
    when rename_to_canonical is True (the duplicate_bills backfill), in which
    case it's renamed ACCOUNT_YYYY_MM_DD_<suffix>.pdf first.

    Returns the "FY<YY>/<account>" folder it went into, or None if skipped.
    """
    match = config.FILENAME_PATTERN.match(pdf_path.name)
    source = pdf_path

    if match:
        acct = match.group("acct")
        file_date = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    elif rename_to_canonical:
        acct = str(account)
        file_date = date_to
        canonical_name = f"{acct}_{date_to.strftime('%Y_%m_%d')}_{config.FILENAME_SUFFIX}.pdf"
        renamed = pdf_path.parent / canonical_name
        counter = 1
        while renamed.exists():
            renamed = pdf_path.parent / f"{Path(canonical_name).stem}_{counter}.pdf"
            counter += 1
        pdf_path.rename(renamed)
        source = renamed
    else:
        print(f"      [WARNING] Unmatched filename, leaving in place: {pdf_path.name}")
        return None

    fy_folder = get_fiscal_year(file_date.year, file_date.month)
    target_dir = Path(config.ARCHIVE_DIR) / fy_folder / acct
    target_dir.mkdir(parents=True, exist_ok=True)

    destination = target_dir / source.name
    counter = 1
    while destination.exists():
        destination = target_dir / f"{source.stem}_{counter}{source.suffix}"
        counter += 1

    shutil.move(str(source), str(destination))
    return f"{fy_folder}/{acct}"


# ── Running a pass over a list of PDFs ───────────────────────────────────

def archive_confirmed_bills(pdf_files, rename_to_canonical):
    """Check each PDF against both tables and archive the confirmed ones."""
    counts = {"ok": 0, "unreadable": 0, "db_error": 0, "no_bills_row": 0, "meters_missing": 0}
    archived = 0
    total = len(pdf_files)

    for number, pdf_path in enumerate(pdf_files, start=1):
        label = f"[{number}/{total}] {pdf_path.name}"
        try:
            status, account, date_to, detail = confirm_in_database(pdf_path)
        except Exception as error:
            print(f"{label} -> ERROR: {error}")
            counts["unreadable"] += 1
            continue

        counts[status] += 1
        if status != "ok":
            print(f"{label} -> left in place: {detail}")
            continue

        folder = archive_pdf(pdf_path, account, date_to, rename_to_canonical)
        if folder:
            print(f"{label} -> archived to {folder} ({detail})")
            archived += 1

    print(f"\nArchived: {archived} of {total}")
    print(f"  Left in place, not in {BILLS_TABLE}:        {counts['no_bills_row']}")
    print(f"  Left in place, meters missing from {METERS_TABLE}: {counts['meters_missing']}")
    print(f"  Left in place, couldn't read PDF:         {counts['unreadable']}")
    print(f"  Left in place, database check failed:     {counts['db_error']}")
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
    print(f"Starting bill archiving (a bill needs rows in {BILLS_TABLE} AND {METERS_TABLE})...")

    # Pass 1: input_bills/
    print("\n=== Pass 1: input_bills/ ===")
    input_pdfs, lpg_skipped = find_input_pdfs()
    if lpg_skipped:
        print(f"Skipping {lpg_skipped} LPG bill(s) -- the LPG scripts archive those.")
    if input_pdfs:
        archive_confirmed_bills(input_pdfs, rename_to_canonical=False)
        remove_empty_subfolders(config.SOURCE_DIR)
    else:
        print("No PDF files found in input_bills/.")

    # Pass 2: duplicate_bills/ backfill
    print("\n=== Pass 2: duplicate_bills/ backfill ===")
    duplicate_dir = Path(config.DUPLICATE_DIR)
    if duplicate_dir.exists():
        duplicate_pdfs = sorted(f for f in duplicate_dir.glob("*.pdf") if f.parent == duplicate_dir)
    else:
        duplicate_pdfs = []
    if duplicate_pdfs:
        archive_confirmed_bills(duplicate_pdfs, rename_to_canonical=True)
    else:
        print("No PDF files found in duplicate_bills/.")

    print("\nDone.")