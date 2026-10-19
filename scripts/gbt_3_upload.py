"""
3) Upload the extracted invoice data to the database -- both tables, one script:

    gas_bill_summary_extracted.csv         -> gas_bill_summary
    gas_bill_meter_readings_extracted.csv  -> gas_bill_meter_readings

Both CSVs come from "2) extract_bill_tables.py" (output folder from config.py).

Only runs where psql can reach the database (the server) -- not from a laptop.
The two uploads are independent, so both are always attempted (summary first,
then meter readings) and a summary is printed at the end. Both are safe to
re-run: rows that already exist are never overwritten.

For each table the script:
  1. stamps every row's updated_at with the current date/time
  2. makes sure the staging table exists, then empties it
  3. \\copy's the CSV into the staging table (and checks the row count matches)
  4. inserts into the real table only the rows that aren't already there
       gas_bill_summary           matched on account_number + invoice_number
       gas_bill_meter_readings    matched on account_number + invoice_number
                                  + meter_id + date_to (a meter can appear twice
                                  on one invoice, e.g. when it is swapped mid-cycle)

This script does NOT archive any PDFs. "4) archive_bill_tables.py" does that,
and only for bills confirmed in both tables.

Run order:  2) extract_bill_tables.py  ->  3) this script  ->  4) archive_bill_tables.py

The staging tables are created automatically the first time (same columns as
the real tables). If a staging table ever gets out of step with its real table,
drop it and the next run re-creates it:
    DROP TABLE gas_bill_summary_staging;
    DROP TABLE gas_bill_meter_readings_staging;

If your tables live in a schema (e.g. gas.gas_bill_summary), put the schema in
front of the four table names below.
"""

import csv
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path so we can import `config`.
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config

DATABASE = "uhm2023"

SUMMARY_TABLE = "gas.gas_bill_summary"
SUMMARY_STAGING = "gas.gas_bill_summary_staging"
SUMMARY_CSV = "gas_bill_summary_extracted.csv"
SUMMARY_COLUMNS = ("account_number, invoice_number, last_bill_date, current_bill_date, due_date, "
                   "previous_balance, payments, current_charges, total_amount_due, total_therms, "
                   "meter_count, pdf_filename, updated_at")
SUMMARY_KEY = ["account_number", "invoice_number"]

METERS_TABLE = "gas.gas_bill_meter_readings"
METERS_STAGING = "gas.gas_bill_meter_readings_staging"
METERS_CSV = "gas_bill_meter_readings_extracted.csv"
METERS_COLUMNS = ("account_number, invoice_number, meter_id, date_from, reading_from, date_to, "
                  "reading_to, usage, days, factor, therms, pdf_filename, updated_at")
METERS_KEY = ["account_number", "invoice_number", "meter_id", "date_to"]


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


def stamp_updated_at(csv_path):
    """
    Stamp every row in the CSV with the current date and time in its updated_at
    column, overwriting the file in place. Done right before the CSV is
    \\copy'd, so updated_at reflects when the upload happened, not when the PDF
    was extracted. Returns True on success.
    """
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if not fieldnames or "updated_at" not in fieldnames:
        print(f"[ERROR] {csv_path.name} has no updated_at column.")
        return False

    for row in rows:
        row["updated_at"] = updated_at

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OK] Stamped {len(rows)} row(s) with updated_at = {updated_at}")
    return True


def upload_table(csv_name, main_table, staging_table, columns, key_columns):
    """Upload one CSV into one table via its staging table. Returns True on success."""
    csv_path = Path(config.OUTPUT_CSV).parent / csv_name
    if not csv_path.is_file():
        print(f"[ERROR] {csv_name} not found in: {csv_path.parent}")
        return False

    with open(csv_path, newline="") as f:
        csv_rows = sum(1 for _ in csv.reader(f)) - 1  # minus header
    print(f"[1/5] Using {csv_path.name} ({csv_rows} rows)")
    if csv_rows <= 0:
        print("[ERROR] The CSV has no data rows. Nothing to upload.")
        return False

    print("[2/5] Stamping updated_at ...")
    if not stamp_updated_at(csv_path):
        return False

    column_list = [c.strip() for c in columns.split(",")]
    select_columns = ", ".join(f"st.{c}" for c in column_list)
    key_list = ", ".join(f"st.{k}" for k in key_columns)
    key_match = " AND ".join(f"s.{k} = st.{k}" for k in key_columns)
    insert_sql = f"""
        INSERT INTO {main_table} ({columns})
        SELECT DISTINCT ON ({key_list})
               {select_columns}
        FROM {staging_table} st
        WHERE NOT EXISTS (
            SELECT 1 FROM {main_table} s
            WHERE {key_match}
        )
        ORDER BY {key_list};
    """

    try:
        print(f"[3/5] Preparing and emptying {staging_table} ...")
        run_psql(f"CREATE TABLE IF NOT EXISTS {staging_table} "
                 f"(LIKE {main_table} INCLUDING DEFAULTS INCLUDING IDENTITY);")
        run_psql(f"TRUNCATE TABLE {staging_table};")

        print(f"[4/5] Copying CSV into {staging_table} ...")
        copy_output = run_psql(rf"""\copy {staging_table}({columns}) FROM '{csv_path}' CSV HEADER;""")
        match = re.search(r"COPY (\d+)", copy_output)
        if match and int(match.group(1)) != csv_rows:
            print(f"[ERROR] CSV has {csv_rows} rows but {match.group(1)} landed in {staging_table}.")
            print(f"        Nothing was inserted into {main_table}.")
            return False

        print(f"[5/5] Inserting new rows into {main_table} ...")
        insert_output = run_psql(insert_sql)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database upload failed (psql exit code {e.returncode}).")
        if e.stderr:
            print(f"        {e.stderr.strip()}")
        print(f"        {csv_path.name} was NOT fully uploaded to {main_table}.")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False

    match = re.search(r"INSERT 0 (\d+)", insert_output)
    if match:
        inserted = int(match.group(1))
        print(f"[OK] Inserted {inserted} new row(s) into {main_table}; "
              f"{csv_rows - inserted} already existed or were duplicates.")
    else:
        print(f"[OK] Uploaded {csv_path.name} to {main_table}")
    return True


if __name__ == "__main__":
    print(f"Starting database upload ({SUMMARY_TABLE}, then {METERS_TABLE})...")

    print(f"\n=== Part 1/2: invoice data -> {SUMMARY_TABLE} ===")
    summary_ok = upload_table(SUMMARY_CSV, SUMMARY_TABLE, SUMMARY_STAGING, SUMMARY_COLUMNS, SUMMARY_KEY)

    print(f"\n=== Part 2/2: meter readings -> {METERS_TABLE} ===")
    meters_ok = upload_table(METERS_CSV, METERS_TABLE, METERS_STAGING, METERS_COLUMNS, METERS_KEY)

    print("\n=== Summary ===")
    print(f"  {SUMMARY_TABLE + ':':<26}{'OK' if summary_ok else 'FAILED'}")
    print(f"  {METERS_TABLE + ':':<26}{'OK' if meters_ok else 'FAILED'}")

    if summary_ok and meters_ok:
        print("\nDone. PDFs were not archived -- run 4) archive_bill_tables.py next.")
    else:
        print("\nUpload did not fully finish. Fix the issue(s) above, then re-run this script "
              "(rows that already uploaded are skipped).")
        sys.exit(1)


r"""

Script verification methodology:

--- create test tables with the exact columns/types of the real ones, but no rows ---

CREATE TABLE gas_bill_summary_test        AS TABLE gas_bill_summary        WITH NO DATA;
CREATE TABLE gas_bill_meter_readings_test AS TABLE gas_bill_meter_readings WITH NO DATA;

--- set SUMMARY_TABLE = "gas_bill_summary_test" and METERS_TABLE =
    "gas_bill_meter_readings_test" at the top of this script (and the two
    *_STAGING names to match, e.g. "gas_bill_summary_test_staging") ---

--- run the script ---

--- check that the rows in both CSVs are in the test tables, and run it a second
    time: it should report 0 new rows inserted ---

"""