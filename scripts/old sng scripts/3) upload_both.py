"""
3) Upload the extracted bill data to the database -- both tables, one script:

    gas_acc_extracted.csv    -> gas.sng_bills  (one row per account per bill)
    gas_meter_extracted.csv  -> gas.sng        (one row per meter per bill)

Both CSVs come from "2) extract_bills.py" (output folder from config.py).

Only runs where psql can reach the database (the server) -- not from a laptop.
The two uploads are independent, so both are always attempted (account first,
then meters) and a summary is printed at the end. Both are safe to re-run:
rows that already exist are never overwritten.

  Part 1/2  gas.sng_bills   uses gas_common.upload_csv_to_database
                            (\\copy into gas.sng_bills_staging, then insert the new rows)
  Part 2/2  gas.sng         stamps upload_time, \\copy's into gas.sng_staging
                            (checking the row count), then inserts only the rows
                            not already there (matched on meter_id + date_to)

This script does NOT archive any PDFs. The archive script does that, and only
for bills confirmed in BOTH gas.sng_bills and gas.sng.

Run order:  2) extract_bills.py  ->  3) this script  ->  archive script

One-time setup, if gas.sng_staging doesn't exist yet (same columns as gas.sng):
    CREATE TABLE gas.sng_staging (LIKE gas.sng INCLUDING DEFAULTS);
"""

import csv
import re
import subprocess
import sys
from pathlib import Path

# Ensure project root is on sys.path so we can import `config` and `gas_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import add_upload_timestamp, upload_csv_to_database

DATABASE = "uhm2023"

# Meter upload settings (gas.sng)
MAIN_TABLE = "gas.sng"
STAGING_TABLE = "gas.sng_staging"
CSV_NAME = "gas_meter_extracted.csv"
COLUMNS = "meter_id, date_from, read1, date_to, read2, days, factor, therms, account, upload_time"


def run_psql(command):
    """Run one psql command and return what it printed. Raises CalledProcessError on failure."""
    result = subprocess.run(
        ["psql", DATABASE, "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    if output:
        print(f"      psql: {output}")
    return output


def upload_meter_csv_to_database():
    """Upload the meter CSV into gas.sng. Returns True on success."""
    csv_path = Path(config.OUTPUT_CSV).parent / CSV_NAME
    if not csv_path.is_file():
        print(f"[ERROR] {CSV_NAME} not found in: {csv_path.parent}")
        return False

    with open(csv_path, newline="") as f:
        csv_rows = sum(1 for _ in csv.reader(f)) - 1  # minus header
    print(f"[1/5] Using {csv_path.name} ({csv_rows} rows)")
    if csv_rows <= 0:
        print("[ERROR] The CSV has no data rows. Nothing to upload.")
        return False

    print("[2/5] Stamping upload_time ...")
    add_upload_timestamp(csv_path)

    insert_sql = f"""
        INSERT INTO {MAIN_TABLE} ({COLUMNS})
        SELECT DISTINCT ON (st.meter_id, st.date_to)
               st.meter_id, st.date_from, st.read1, st.date_to, st.read2,
               st.days, st.factor, st.therms, st.account, st.upload_time
        FROM {STAGING_TABLE} st
        WHERE NOT EXISTS (
            SELECT 1 FROM {MAIN_TABLE} s
            WHERE s.meter_id = st.meter_id
              AND s.date_to = st.date_to
        )
        ORDER BY st.meter_id, st.date_to;
    """

    try:
        print(f"[3/5] Emptying {STAGING_TABLE} ...")
        run_psql(f"TRUNCATE TABLE {STAGING_TABLE};")

        print(f"[4/5] Copying CSV into {STAGING_TABLE} ...")
        copy_output = run_psql(rf"""\copy {STAGING_TABLE}({COLUMNS}) FROM '{csv_path}' CSV HEADER;""")
        match = re.search(r"COPY (\d+)", copy_output)
        if match and int(match.group(1)) != csv_rows:
            print(f"[ERROR] CSV has {csv_rows} rows but {match.group(1)} landed in {STAGING_TABLE}.")
            print(f"        Nothing was inserted into {MAIN_TABLE}.")
            return False

        print(f"[5/5] Inserting new rows into {MAIN_TABLE} ...")
        insert_output = run_psql(insert_sql)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database upload failed (psql exit code {e.returncode}).")
        if e.stderr:
            print(f"        {e.stderr.strip()}")
        print(f"        {csv_path.name} was NOT fully uploaded to {MAIN_TABLE}.")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False

    match = re.search(r"INSERT 0 (\d+)", insert_output)
    if match:
        inserted = int(match.group(1))
        print(f"[OK] Inserted {inserted} new row(s) into {MAIN_TABLE}; "
              f"{csv_rows - inserted} already existed or were duplicates.")
    else:
        print(f"[OK] Uploaded {csv_path.name} to {MAIN_TABLE}")
    return True


if __name__ == "__main__":
    print("Starting database upload (gas.sng_bills, then gas.sng)...")

    print("\n=== Part 1/2: account data -> gas.sng_bills ===")
    accounts_ok = upload_csv_to_database()

    print("\n=== Part 2/2: meter data -> gas.sng ===")
    meters_ok = upload_meter_csv_to_database()

    print("\n=== Summary ===")
    print(f"  gas.sng_bills: {'OK' if accounts_ok else 'FAILED'}")
    print(f"  gas.sng:       {'OK' if meters_ok else 'FAILED'}")

    if accounts_ok and meters_ok:
        print("\nDone. PDFs were not archived -- run the archive script next.")
    else:
        print("\nUpload did not fully finish. Fix the issue(s) above, then re-run this script "
              "(rows that already uploaded are skipped).")
        sys.exit(1)


r"""

Script verification methodology:

--- create test tables with the exact columns/types of the real ones, but no rows ---

CREATE TABLE gas.sng_bills_test AS TABLE gas.sng_bills WITH NO DATA;
CREATE TABLE gas.sng_test       AS TABLE gas.sng       WITH NO DATA;

--- point the uploads at the test tables: MAIN_TABLE = "gas.sng_test" at the top
    of this script, and the insert in gas_common.upload_csv_to_database ---

--- run the script ---

--- check that the rows in both CSVs are in the test tables, and run it a second
    time: it should report 0 new rows inserted ---

"""