"""
1) Organize gas bills.

All the actual logic now lives in gas_common.py, which sits directly in
the gas_bills/ folder.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent      # gas_bills/scripts/  <- where config.py lives
GAS_BILLS_ROOT = SCRIPTS_DIR.parent  # gas_bills/  <- where gas_common.py lives
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import organize_bills, load_max_date, filter_already_billed_by_date


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else str(config.SOURCE_DIR)

    print("Gas Bill Organizer")
    print("=" * 70)
    # print(f"Working folder:\n  {folder}\n")

    max_date = load_max_date(config.MAX_DATE_CSV_LPG)
    filter_already_billed_by_date(
        folder_path=folder,
        duplicate_dir=config.DUPLICATE_DIR,
        max_date=max_date,
        get_bill_date=config.get_bill_date,
    )

    organize_bills(
        folder_path=folder,
        archive_dir=config.ARCHIVE_DIR,
        duplicate_dir=config.DUPLICATE_DIR,
        filename_suffix=config.FILENAME_SUFFIX,
        extract_bill_info=config.extract_bill_info,
    )


if __name__ == "__main__":
    main()
