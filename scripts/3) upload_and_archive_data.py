"""
3) Upload gas bill data to the database, then archive the bills —
   but ONLY if the upload succeeded.

Only runs where psql can reach the database (the server) — not from a
laptop, same as script 0). Calls psql to \\copy the extracted CSV into
the `gas.sng_bills` table; if that succeeds, moves the corresponding bills
from input_bills/ into bill_archive/FY<YY>/ (see gas_common.upload_and_archive).
If the upload fails, the bills are left in input_bills/ untouched so a
fix + re-run can't skip any bill's database row.

Paths/database/table/columns all come from config.py, same as script 1).
"""

import sys
import importlib.util
from pathlib import Path

# Ensure project root is on sys.path so we can import `gas_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import upload_and_archive, archive_duplicates_already_in_database

# "2) gas_bill_extract.py" isn't a valid module name to `import` directly
# (space + parenthesis), so load it by path to reuse get_account_and_date_to.
_extract_spec = importlib.util.spec_from_file_location(
    "gas_bill_extract", SCRIPT_DIR / "2) gas_bill_extract.py"
)
gas_bill_extract = importlib.util.module_from_spec(_extract_spec)
_extract_spec.loader.exec_module(gas_bill_extract)


if __name__ == "__main__":
    upload_and_archive(
        source_dir=config.SOURCE_DIR,
        archive_dir=config.ARCHIVE_DIR,
        filename_pattern=config.FILENAME_PATTERN,
    )

    # Backfill sweep: anything sitting in duplicate_bills/ whose data is
    # already confirmed in gas.sng_bills gets moved into the archive too
    # (e.g. bills uploaded before this archiving system existed).
    print("\nChecking duplicate_bills/ for bills to backfill into the archive...")
    archive_duplicates_already_in_database(
        duplicate_dir=config.DUPLICATE_DIR,
        archive_dir=config.ARCHIVE_DIR,
        filename_pattern=config.FILENAME_PATTERN,
        filename_suffix=config.FILENAME_SUFFIX,
        get_account_and_date_to=gas_bill_extract.get_account_and_date_to,
    )
    

r"""

Script verification methodology:

--- create new table in database with exact column names and column data types, but ignore the existing values ---

CREATE TABLE gas.sng_bills_test AS 
TABLE gas.sng_bills 
WITH NO DATA;

--- update the script above to upload into the test table ---
\copy gas.sng_bills_test(account, date_from, date_to, therms, therms_adjusted, "billed_$", note) FROM 'gas_bills/output/gas_bills_extracted.csv' CSV HEADER;

--- run the script ---

--- check if the values in gas_bills_extracted.csv are in the gas.sng_bills_test table ---

"""