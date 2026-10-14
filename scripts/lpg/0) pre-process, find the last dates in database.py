"""
0) Pre-process: pull the max billed-through date from the database.

Only runs where psql can reach the database (the server) — not from a
laptop. Refreshes extracts/gas_bill_max_date.csv so later steps
can see what's already in the database before processing new bills.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so we can import `gas_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent      # gas_bills/scripts/  <- where config.py lives
GAS_BILLS_ROOT = SCRIPTS_DIR.parent  # gas_bills/  <- where gas_common.py lives
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(GAS_BILLS_ROOT))

import config
from gas_common import extract_query_to_csv


if __name__ == "__main__":

    # for sng database
    extract_query_to_csv(
        database=config.DATABASE,
        query=config.MAX_DATE_QUERY,
        csv_path=config.MAX_DATE_CSV,
    )

    # for lpg database
    extract_query_to_csv(
        database=config.DATABASE,
        query=config.MAX_DATE_QUERY_LPG,
        csv_path=config.MAX_DATE_CSV_LPG,
    )
    