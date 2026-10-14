"""
config.py - everything specific to the GAS bill pipeline.

PATHS ARE PORTABLE:
This finds the "gas_bills" root automatically, based on where THIS file
sits, so the same code works on your laptop and later on the server
without editing any paths.

Actual folder layout this expects:

    gas_bills/
    ├── scripts/
    │   ├── config.py                       <- this file
    │   ├── 1) organize_gas_bills.py
    │   ├── 2) gas_bill_extract.py
    │   ├── 3) upload_and_archive_data.py
    ├── input_bills/
       └── duplicate_bills
    ├── bill_archive/
    └── output/
       └── gas_bills_extracted.csv
"""

import re
from pathlib import Path
from datetime import date, datetime
import pymupdf

# ── Find folders automatically, relative to this file ────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent      # .../gas_bills/scipts
PROJECT_DIR = SCRIPT_DIR.parent                   # .../gas_bills/
GAS_BILLS_ROOT = PROJECT_DIR                     # same as project directory

# ── Paths ──────────────────────────────────────────────────────────────
SOURCE_DIR = PROJECT_DIR / "input_bills"
ARCHIVE_DIR = PROJECT_DIR / "bill_archive"
DUPLICATE_DIR = SOURCE_DIR / "duplicate_bills"
OUTPUT_CSV = PROJECT_DIR / "output" / "gas_bills_extracted.csv"
OUTPUT_LPG_CSV = PROJECT_DIR / "output" / "lpg_bills_extracted.csv"

# ── Filename ──────────────────────────────────────────────────────────
# Canonical format: ACCOUNTNUMBER_YYYY_MM_DD_gas_bill.pdf
FILENAME_SUFFIX = "gas_bill"

FILENAME_PATTERN = re.compile(
    r"^(?P<acct>\d+)_(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2}).*\.pdf$",
    re.IGNORECASE,
)

# ── Database (step 4) ────────────────────────────────────────────────
DATABASE = "uhm2023"
TABLE = "gas.sng_bills"
COLUMNS = [
    "account",
    "date_from",
    "date_to",
    "therms",
    "therms_adjusted",
    "billed_$",
    "note",
    "upload_time",
]

# ── Database (step 0) ─────────────────────────────────────────────────
EXTRACTS_DIR = GAS_BILLS_ROOT / "extracts"

# gets the max date across all accounts so that you don't miss a month
MAX_DATE_QUERY = """
WITH account_start AS (
    SELECT account,
           date_trunc('month', MIN(date_from))::date AS first_month
    FROM gas.sng_bills
    GROUP BY account
),
months AS (
    SELECT mo::date AS month
    FROM calendar.fy_mo
    WHERE mo >= date_trunc('month', CURRENT_DATE - INTERVAL '1 year')
      AND mo <= date_trunc('month', CURRENT_DATE)
),
expected AS (
    SELECT a.account, m.month
    FROM account_start a
    JOIN months m ON m.month >= a.first_month
),
actual AS (
    SELECT account,
           date_trunc('month', date_to)::date AS month
    FROM gas.sng_bills
    GROUP BY account, date_trunc('month', date_to)
),
gaps AS (
    SELECT e.month AS missing_month
    FROM expected e
    LEFT JOIN actual a
           ON a.account = e.account
          AND a.month = e.month
    WHERE a.account IS NULL
),
fallback AS (
    SELECT MIN(last_date) AS max
    FROM (
        SELECT account, MAX(date_to) AS last_date
        FROM gas.sng_bills
        GROUP BY account
    ) AS per_account_max
)
SELECT COALESCE(
    (SELECT (MIN(missing_month) - INTERVAL '1 month')::date FROM gaps),
    (SELECT max FROM fallback)
) AS max
"""

MAX_DATE_CSV = EXTRACTS_DIR / "gas_bill_max_date.csv"

# max date for lpg bills
MAX_DATE_CSV_LPG = EXTRACTS_DIR / "lpg_bill_max_date.csv"

# gets the max date across all accounts so that you don't miss a month
MAX_DATE_QUERY_LPG = """
WITH account_start AS (
    SELECT account,
           date_trunc('month', MIN(date))::date AS first_month
    FROM gas.lpg_bills
    GROUP BY account
),
months AS (
    SELECT mo::date AS month
    FROM calendar.fy_mo
    WHERE mo >= date_trunc('month', CURRENT_DATE - INTERVAL '1 year')
      AND mo <= date_trunc('month', CURRENT_DATE)
),
expected AS (
    SELECT a.account, m.month
    FROM account_start a
    JOIN months m ON m.month >= a.first_month
),
actual AS (
    SELECT account,
           date_trunc('month', date)::date AS month
    FROM gas.lpg_bills
    GROUP BY account, date_trunc('month', date)
),
gaps AS (
    SELECT e.month AS missing_month
    FROM expected e
    LEFT JOIN actual a
           ON a.account = e.account
          AND a.month = e.month
    WHERE a.account IS NULL
),
fallback AS (
    SELECT MIN(last_date) AS max
    FROM (
        SELECT account, MAX(date) AS last_date
        FROM gas.lpg_bills
        GROUP BY account
    ) AS per_account_max
)
SELECT COALESCE(
    (SELECT (MIN(missing_month) - INTERVAL '1 month')::date FROM gaps),
    (SELECT max FROM fallback)
) AS max
"""

# ── PDF parsing (project-specific — every bill layout is different) ───
def extract_bill_info(pdf_path):
    """
    Parsing logic for gas bills: pulls raw text with pymupdf and
    returns (account_number, billing_date) where billing_date is a
    datetime.date, or (None, None) if either can't be found/parsed.
    """
    try:
        doc = pymupdf.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
    except Exception as e:
        print(f"  [ERROR] Reading {pdf_path.name}: {e}")
        return None, None

    if not text:
        return None, None

    acct_match = re.search(r"Account\s+Number\s*[:\-]?\s*(\d+)", text, re.IGNORECASE)
    account_number = acct_match.group(1) if acct_match else None

    date_match = re.search(r"Current\s+Bill\s+Date\s*[:\-]?\s*([A-Za-z0-9 ,/-]+)", text, re.IGNORECASE)
    billing_date = None
    if date_match:
        raw_date = date_match.group(1).strip()
        for fmt in ("%m/%d/%Y", "%B %d, %Y", "%Y-%m-%d"):
            try:
                billing_date = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue

    return account_number, billing_date

def get_bill_date(pdf_path):
    """
    Extract the gas bill's Current Bill Date as a datetime.date, for
    checking against the max-date extract from script 0.
    """
    _, billing_date = extract_bill_info(pdf_path)
    return billing_date

def cleanup_old_csvs(output_path, keep=5):
    """Delete the oldest extracted CSVs, keeping only the `keep` most recent.

    Gas and LPG timestamped CSVs share one pool, so `keep` applies to the
    output folder as a whole, not to each type separately.
    """
    backups = sorted(
        [p for p in output_path.parent.glob(f"*_bills_extracted_*{output_path.suffix}") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_csv in backups[keep:]:
        print(f"Removing old CSV: {old_csv.name}")
        old_csv.unlink()