"""
lpg_bill_extract.py
Extract Hawaii Gas *LPG* bills (Service Type: Non-Utility LPG Tank) into rows
matching gas.lpg_bills: account, date, gallons, billed_$

Differences from SNG bills:
  - no meter rows / therms / read dates
  - date   = "Current Bill Date" from the Account Summary
  - gallons = value(s) in the Gallons column of the delivery row (0 if no delivery)
  - billed_$ = "Net Current Charges After Discount"
"""
import glob
import re
import sys
from datetime import datetime
from pathlib import Path
import pandas as pd
import pdfplumber

# Needed in order to import config
SCRIPT_DIR = Path(__file__).resolve().parent
GAS_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from config import cleanup_old_csvs


# Columns of gas.lpg_bills (in table order)
LPG_COLUMNS = ["account", "date", "gallons", "billed_$"]

MONEY = r"\$?\(?([\d,]+\.\d{2})\)?"


def _money(text, label):
    """Return the float that follows `label` on the same line, or None."""
    m = re.search(rf"{re.escape(label)}\s+{MONEY}", text)
    return float(m.group(1).replace(",", "")) if m else None


def _gallons(page):
    """Sum numbers sitting under the 'Gallons' column of the delivery table."""
    words = page.extract_words()
    # The delivery-table header is the 'Gallons' on the same line as 'Ticket'
    header = next(
        (w for w in words if w["text"] == "Gallons"
         and any(t["text"] == "Ticket" and abs(t["top"] - w["top"]) < 3 for t in words)),
        None,
    )
    if header is None:
        return 0.0
    # Delivery rows live between that header and the 'Current Charges' title
    stop = next((w["top"] for w in words if w["text"] == "Current" and w["top"] > header["top"]), 1e9)
    total = 0.0
    for w in words:
        in_rows = header["bottom"] < w["top"] < stop
        in_col = abs((w["x0"] + w["x1"]) / 2 - (header["x0"] + header["x1"]) / 2) < 30
        if in_rows and in_col and re.fullmatch(r"[\d,]*\.?\d+", w["text"]):
            total += float(w["text"].replace(",", ""))
    return total


def extract_pdf(pdf_path):
    """Return a list with one row dict for lpg_bills, or [] if this isn't an LPG bill."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        if "LPG" not in text:
            return []

        acct = re.search(r"Account Number\s+(\d+)", text)
        bill_date = re.search(r"Current Bill Date\s+(\d{2}/\d{2}/\d{4})", text)
        if not (acct and bill_date):
            raise ValueError("Couldn't find account number / bill date")

        return [{
            "account": acct.group(1),
            "date": datetime.strptime(bill_date.group(1), "%m/%d/%Y").date(),
            "gallons": _gallons(page),
            "billed_$": _money(text, "Net Current Charges After Discount"),
        }]


def extract_all_bills(input_folder):
    """Extract all LPG PDFs under input_folder except files in duplicates."""
    duplicate_dir = Path(config.DUPLICATE_DIR).resolve()

    all_pdfs = [Path(p) for p in glob.glob(str(Path(input_folder) / "**" / "*lpg_bill*"), recursive=True)]

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

    for i, pdf_path in enumerate(pdf_files, 1):
        # print(f"[{i}/{len(pdf_files)}] {pdf_path.name} ...", end=" ")
        try:
            rows = extract_pdf(pdf_path)
        except Exception as error:
            print(f"ERROR: {error}")
            continue
        if not rows:
            print("skipped (not an LPG bill)")
            continue
        all_rows.extend(rows)
        r = rows[0]
        # print(f"OK  {r['account']}  {r['date']}  {r['gallons']} gal  ${r['billed_$']}")

    print(f"Extracted {len(all_rows)} LPG bill row(s) from {len(pdf_files)} PDFs")
    return pd.DataFrame(all_rows, columns=LPG_COLUMNS)

def get_account_and_date(pdf_path):
    """Lightweight (account, bill_date) reader for an LPG bill PDF, or (None, None).
    Used by gas_common.archive_duplicates_already_in_database_lpg()."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return None, None
    if "LPG" not in text:
        return None, None
    acct = re.search(r"Account Number\s+(\d+)", text)
    bill_date = re.search(r"Current Bill Date\s+(\d{2}/\d{2}/\d{4})", text)
    if not (acct and bill_date):
        return None, None
    return acct.group(1), datetime.strptime(bill_date.group(1), "%m/%d/%Y").date()

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "."
    df = extract_all_bills(config.SOURCE_DIR)
    if df.empty:
        print("No PDFs to extract -- skipping CSV write.")
        print("Aborting to avoid uploading an older CSV; exiting with non-zero status.")
        sys.exit(1)
    else:
        output_path = Path(config.OUTPUT_LPG_CSV)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        timestamped_path = output_path.parent / f"{output_path.stem}_{timestamp}{output_path.suffix}"
        df.to_csv(timestamped_path, index=False)
        cleanup_old_csvs(output_path)

        print(f"Processing complete. CSV saved to {timestamped_path}")