"""
2) Extract gas bill data into a CSV to then be uploaded to gas.sng table.

The PDF parsing logic follows the original SNG gas bill extractor. Paths and
output columns come from config.py so this script can run on any machine.

Parses therms from each meter and creates one row per meter.

Therms is the value PRINTED on the bill for each meter. Two checks run on
every bill:
  1. Each meter: (reading_to - reading_from) * factor is close to printed therms.
  2. Each bill: the meter therms add up to the bill's "Consumption Charge" therms.
Problems are printed as warnings and listed at the end. Rows are still written
so you can look at them before anything is uploaded.
"""

import glob
import re
import sys
from pathlib import Path
import pandas as pd
from pypdf import PdfReader

# Needed in order to import config
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config


ACCOUNT_REGEX = r"Account Number\s+(\d+)"

# Account-wide total therms, printed once in the "Current Charges" table.
# Only used here to double-check that the meters add up.
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


def read_bill(pdf_path):
    """Read one PDF. Returns (account, bill_total_therms, raw_meter_rows)."""
    account = None
    bill_total = None
    raw_rows = []

    with open(pdf_path, "rb") as pdf_file:
        reader = PdfReader(pdf_file)

        for page in reader.pages:
            text = page.extract_text() or ""

            if account is None:
                match = re.search(ACCOUNT_REGEX, text)
                if match:
                    account = match.group(1)

            if bill_total is None:
                match = re.search(TOTAL_THERMS_REGEX, text)
                if match is None:
                    match = re.search(r"Consumption\s+([\d,]+\.\d{2})", text)
                if match:
                    bill_total = float(match.group(1).replace(",", ""))

            # Meter rows can be on any page (large accounts spill onto page 2)
            for row_match in re.finditer(METER_ROW_REGEX, text):
                raw_rows.append(row_match.groups())

    return account, bill_total, raw_rows


def build_records(account, raw_rows):
    """Turn raw regex groups into row dicts. Returns (records, meter_issues)."""
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


def extract_all_meters(input_folder):
    """Extract meter rows from every PDF under input_folder except duplicates."""
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

    all_rows = []
    problems = []

    for number, pdf_path in enumerate(pdf_files, start=1):
        label = f"[{number}/{total}] {pdf_path.name}"

        try:
            account, bill_total, raw_rows = read_bill(pdf_path)
        except Exception as error:
            print(f"{label} -> ERROR: {error}")
            problems.append(f"{pdf_path.name}: could not be read ({error})")
            continue

        if account is None or not raw_rows:
            print(f"{label} -> WARNING: no account/meter rows found, skipped")
            problems.append(f"{pdf_path.name}: no account or meter rows found")
            continue

        records, meter_issues = build_records(account, raw_rows)
        meter_sum = round(sum(r["therms"] for r in records), 2)
        issues = list(meter_issues)

        if bill_total is None:
            print(f"{label} -> {len(records)} meters, {meter_sum:,.2f} therms "
                  f"(no consumption charge line to compare against)")
        elif abs(meter_sum - bill_total) > BILL_TOLERANCE:
            issues.append(
                f"meters add up to {meter_sum:,.2f} therms but the bill total is {bill_total:,.2f}"
            )
            print(f"{label} -> {len(records)} meters, WARNING: total mismatch")
        else:
            print(f"{label} -> {len(records)} meters, {meter_sum:,.2f} therms, matches bill total")

        for issue in issues:
            print(f"      [WARNING] {issue}")
            problems.append(f"{pdf_path.name}: {issue}")

        all_rows.extend(records)

    print(f"Extracted {len(all_rows)} meter rows from {total} PDFs")
    return pd.DataFrame(all_rows, columns=METER_COLUMNS), problems


if __name__ == "__main__":
    print("Starting per-meter extraction from gas bill PDFs...")
    dataframe, problems = extract_all_meters(config.SOURCE_DIR)

    if dataframe.empty:
        print("No meter rows extracted -- skipping CSV write.")
        sys.exit(1)

    # The file is always named gas_meter_extracted.csv and is overwritten on every run.
    output_dir = Path(config.OUTPUT_CSV).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "gas_meter_extracted.csv"
    dataframe.to_csv(output_path, index=False)

    print(f"Processing complete. CSV saved to {output_path}")
    if problems:
        print(f"\n{len(problems)} item(s) to review before uploading:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("All bills passed both checks.")