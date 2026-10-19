"""
3) Upload gas bill data (one row per account per bill) to gas.sng_bills.

Only runs where psql can reach the database (the server) -- not from a
laptop, same as script 0). Calls psql to \\copy the extracted CSV into
gas.sng_bills_staging, then inserts the new rows into gas.sng_bills
(see gas_common.upload_csv_to_database).

This script does NOT archive any bills. Archiving lives in
"4) archive_bills.py", which only archives a bill once it is confirmed in
BOTH gas.sng_bills (this script) and gas.sng (script 3b).

Run order:  2) extract  ->  2b) meter extract  ->  3) and 3b) uploads  ->  4) archive

Paths/database/table/columns all come from config.py, same as script 1).
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so we can import `gas_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

from gas_common import upload_csv_to_database


if __name__ == "__main__":
    print("Starting upload to gas.sng_bills...")
    if upload_csv_to_database():
        print("\nUpload succeeded. Bills were not archived -- run 3b) for gas.sng, then 4) to archive.")
    else:
        print("\nUpload failed. Fix the issue above, then re-run this script.")
        sys.exit(1)


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