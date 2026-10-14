"""
gas_common.py

Shared toolbox for the gas bills.

Each project keeps its OWN:
    - config.py                    -> paths, filename suffix, DB table/columns
    - N) *_bill_extract.py         -> project-specific PDF data extraction (unchanged)
    - an extract_bill_info() function -> project-specific account/date regex

This module holds the logic that used to be copy-pasted three times with
small, drifting differences:
    - MD5 duplicate detection
    - renaming into canonical ACCOUNT_YYYY_MM_DD_<suffix>.pdf format
    - checking the archive for duplicates / moving duplicates aside
    - moving unique bills into bill_archive/FY<YY>/
    - uploading a CSV to the database via `psql \\copy`

Fix a bug here once, and the gas bill pipeline gets the fix.
"""

import csv
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, date
from pathlib import Path
import tempfile


# ── Duplicate detection ──────────────────────────────────────────────────

def get_pdf_hash(pdf_path):
    """Calculate MD5 hash of a file, used for exact duplicate detection."""
    md5_hash = hashlib.md5()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def bill_exists_in_archive(filename, archive_dir):
    """Check if a bill with this canonical name already exists in any FY folder."""
    archive_dir = Path(archive_dir)
    if not archive_dir.exists():
        return False
    # The archive layout is archive_dir/FY<YY>/<account>/<filename>, so search
    # recursively for the filename anywhere under the archive directory.
    for p in archive_dir.rglob(filename):
        if p.is_file():
            return True
    return False


def move_to_duplicate(pdf_path, duplicate_dir):
    """Move a file into the configured duplicate_bills folder."""
    duplicate_dir = Path(duplicate_dir)
    duplicate_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = Path(pdf_path)
    dst_path = duplicate_dir / pdf_path.name
    counter = 1
    while dst_path.exists():
        dst_path = duplicate_dir / f"{pdf_path.stem}_{counter}{pdf_path.suffix}"
        counter += 1

    shutil.move(str(pdf_path), str(dst_path))
    print(f"DUPLICATE moved: {dst_path.name}")
    return dst_path


# ── Step 0.5: Skip bills already in the database ─────────────────────────

def load_max_dates(csv_path):
    """
    Read the extract CSV (account_no, max) produced by script 0) into a
    dict mapping account number (str) -> the latest date already in the
    database for that account. Example extract path: extracts/gas_bill_max_date.csv.

    Returns an empty dict (nothing gets filtered) if the file doesn't exist
    yet — e.g. script 0) hasn't been run on the server.
    """
    csv_path = Path(csv_path)
    max_dates = {}

    if not csv_path.exists():
        print(f"[WARNING] {csv_path} not found. ")
        print(f"Skipping already-billed check.")
        return max_dates

    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            account_no = (row.get("account_no") or "").strip()
            max_date = (row.get("max") or "").strip()
            if account_no and max_date:
                max_dates[account_no] = datetime.strptime(max_date, "%Y-%m-%d").date()

    return max_dates


def filter_already_billed(folder_path, duplicate_dir, max_dates, get_account_no_and_date):
    """
    Move PDFs whose bill date is not AFTER the max date already in the
    database for that account into duplicate_dir, before organize_bills()
    ever renames or archives them.

    get_account_no_and_date: a function(pdf_path) -> (account_no, bill_date)
                              bill_date is a datetime.date, or None if it
                              couldn't be read. Project-specific, since every
                              gas bill's account number and bill date may
                              be in different locations in the PDF.
    """
    folder = Path(folder_path)
    duplicate_dir = Path(duplicate_dir)

    if not folder.exists():
        return 0

    if max_dates is None:
        return 0

    pdf_files = [f for f in folder.glob("*.pdf") if f.parent == folder]
    moved = 0

    for pdf_file in pdf_files:
        account_no, bill_date = get_account_no_and_date(pdf_file)

        if not account_no or not bill_date:
            print(f"  [WARNING] Could not determine account/date for {pdf_file.name}. Skipping already-billed check.")
            continue

        max_date = max_dates.get(account_no)
        if max_date is not None and bill_date <= max_date:
            print(
                f"Already in database (bill date {bill_date} <= max {max_date} "
                f"for account {account_no}):\n  {pdf_file.name}"
            )
            move_to_duplicate(pdf_file, duplicate_dir)
            moved += 1

    print(f"Already-billed check: {moved} bill(s) moved to duplicates.\n")
    return moved


def load_max_date(csv_path):
    """
    Read a single-value extract (one "max" column, one row), produced by
    a script 0) that queries e.g. SELECT max(date_to) FROM gas.sng_bills,
    into a single datetime.date.

    Use this (instead of load_max_dates) when the database only tracks one
    overall max date rather than one per account.

    Returns None (nothing gets filtered) if the file doesn't exist yet, or
    the value is blank — e.g. script 0) hasn't been run on the server, or
    the table is empty.
    """
    csv_path = Path(csv_path)

    if not csv_path.exists():
        print(f"[WARNING] {csv_path} not found. \nSkipping already-billed check.")
        return None

    with open(csv_path, newline="") as f:
        row = next(csv.DictReader(f), None)

    max_date = (row.get("max") or "").strip() if row else ""
    if not max_date:
        return None

    return datetime.strptime(max_date, "%Y-%m-%d").date()


def filter_already_billed_by_date(folder_path, duplicate_dir, max_date, get_bill_date):
    """
    Move PDFs whose bill date is not AFTER max_date into duplicate_dir,
    before organize_bills() ever renames or archives them.

    Use this (instead of filter_already_billed) when the database only
    tracks one overall max date rather than one per account
    (see load_max_date).

    get_bill_date: a function(pdf_path) -> datetime.date, or None if it
                   couldn't be read. Project-specific.
    """
    folder = Path(folder_path)
    duplicate_dir = Path(duplicate_dir)

    if not folder.exists():
        return

    if max_date is None:
        # debug message to explain that there's not max date so skipping
        # print("Already-billed check: no max date available, skipping.\n")
        return

    pdf_files = [f for f in folder.glob("*.pdf") if f.parent == folder]
    moved = 0

    for pdf_file in pdf_files:
        bill_date = get_bill_date(pdf_file)

        if not bill_date:
            print(f"  [WARNING] Could not determine bill date for {pdf_file.name}. Skipping already-billed check.")
            continue

        if bill_date <= max_date:
            print(f"Already in database (bill date {bill_date} <= max {max_date}):\n  {pdf_file.name}")
            move_to_duplicate(pdf_file, duplicate_dir)
            moved += 1

    print(f"Already-billed check: {moved} bill(s) moved to duplicates.\n")


# ── Step 1: Organize (rename + dedupe) ───────────────────────────────────

LPG_ACCOUNT = "2086592"  # account number for LPG bills, used in canonical filename
LPG_SUFFIX = "lpg_bill"  # suffix for canonical filename of LPG bills

def get_filename_suffix(account, default_suffix):
    """Return 'lpg_bill' for LPG accounts, otherwise the project's default suffix."""
    return LPG_SUFFIX if str(account) == LPG_ACCOUNT else default_suffix

def organize_bills(folder_path, archive_dir, duplicate_dir, filename_suffix, extract_bill_info, already_billed_count=0):
    """
    Rename PDFs into canonical ACCOUNT_YYYY_MM_DD_<suffix>.pdf format.
    Move exact duplicates (by file hash) and bills already present in the
    archive into duplicate_dir.

    extract_bill_info: a function(pdf_path) -> (account_number, date_str)
                        This is the one piece that stays project-specific,
                        since every bill layout is different. Pass in the
                        project's own extraction function.
    """
    folder = Path(folder_path)
    archive_dir = Path(archive_dir)
    duplicate_dir = Path(duplicate_dir)

    if not folder.exists():
        print(f"[ERROR] Folder does not exist:\n  {folder}")
        return

    duplicate_dir.mkdir(parents=True, exist_ok=True)
    # debug print (in case it's looking at wrong folder or saying files not found)
    # print(f"Processing PDFs in: {folder}")
    # print(f"Duplicate folder: {duplicate_dir}\n")

    duplicate_dir_resolved = duplicate_dir.resolve()
    # Recursively scan the provided folder for PDFs, but exclude any files
    # that live under the duplicate_dir so we don't reprocess moved files.
    pdf_files = [
        f for f in folder.rglob("*.pdf")
        if duplicate_dir_resolved not in f.resolve().parents
    ]
    if not pdf_files:
        print("No PDF files found in the folder.")
        return

    # debug output to help figure out where the error is coming from
    # print("=" * 70)
    # print("STEP 1: Analyze PDFs")
    # print("=" * 70)

    file_info = {}
    hash_map = {}
    already_organized = 0

    for pdf_file in pdf_files:
        account_number, date_str = extract_bill_info(pdf_file)

        # Normalize the extracted date into the canonical YYYY_MM_DD form
        if isinstance(date_str, (datetime, date)):
            date_part = date_str.strftime("%Y_%m_%d")
        else:
            date_part = str(date_str).replace("-", "_")

        if not account_number or not date_str:
            print(f"  [WARNING] Could not extract info from {pdf_file.name}. Skipping.")
            file_info[pdf_file] = {"valid": False}
            continue

        canonical_name = f"{account_number}_{date_part}_{get_filename_suffix(account_number, filename_suffix)}.pdf"
        if pdf_file.name == canonical_name and pdf_file.parent == folder / str(account_number):
            already_organized += 1
            # This file is already in the correct account subfolder with the
            # canonical name; skip further processing for it.
            continue
        file_hash = get_pdf_hash(pdf_file)

        file_info[pdf_file] = {
            "valid": True,
            "canonical_name": canonical_name,
            "account": account_number,
        }
        hash_map.setdefault(file_hash, []).append(pdf_file)

    # debug output to help figure out where the error is coming from
    # print("\n" + "=" * 70)
    # print("STEP 2: Move exact duplicate PDFs")
    # print("=" * 70)

    duplicates_moved = already_billed_count
    for file_hash, files in hash_map.items():
        if len(files) <= 1:
            continue

        print(f"\nFound {len(files)} identical files")
        keep_file = files[0]
        print(f"  Keeping: {keep_file.name}")

        for dup_file in files[1:]:
            info = file_info.get(dup_file)
            try:
                if info and info.get("valid"):
                    canonical_name = info["canonical_name"]
                    temp_path = dup_file.parent / canonical_name
                    if dup_file.name != canonical_name:
                        if temp_path.exists():
                            temp_path = dup_file.parent / f"TEMP_{dup_file.name}"
                        dup_file.rename(temp_path)
                        dup_file = temp_path
                move_to_duplicate(dup_file, duplicate_dir)
                duplicates_moved += 1
            except Exception as e:
                print(f"  [ERROR] Moving duplicate: {e}")

    # debug output to help figure out where the error is coming from
    # print("\n" + "=" * 70)
    # print("STEP 3: Rename files")
    # print("=" * 70)

    renamed_count = 0
    skipped_count = 0

    for pdf_file, info in file_info.items():
        if not pdf_file.exists():
            continue
        if not info.get("valid"):
            print(f"\nSkipping invalid file: {pdf_file.name}")
            skipped_count += 1
            continue

        canonical_name = info["canonical_name"]
        canonical_path = pdf_file.parent / canonical_name

        if bill_exists_in_archive(canonical_name, archive_dir):
            print(f"\nDuplicate found in archive:\n  {canonical_name}")
            if pdf_file.name != canonical_name:
                temp_path = canonical_path
                counter = 1
                while temp_path.exists():
                    temp_path = pdf_file.parent / f"{canonical_path.stem}_TEMP{counter}.pdf"
                    counter += 1
                pdf_file.rename(temp_path)
                pdf_file = temp_path
            move_to_duplicate(pdf_file, duplicate_dir)
            duplicates_moved += 1
            continue

        # debug to figure out which files are already named correctly
        # if pdf_file.name == canonical_name:
        #     print(f"\nAlready correctly named:\n  {pdf_file.name}")
        #     continue

        try:
            # After renaming, move the canonical file into an account subfolder
            account = info.get("account")
            account_dir = folder / str(account)
            account_dir.mkdir(parents=True, exist_ok=True)
            dest_in_account = account_dir / canonical_name
            is_duplicate = False
            if dest_in_account.exists() and dest_in_account.resolve() != pdf_file.resolve():
                # print(f"Duplicate found in account folder:\n  {dest_in_account}")
                move_to_duplicate(pdf_file, duplicate_dir)
                duplicates_moved += 1
                is_duplicate = True
            elif pdf_file.resolve() != dest_in_account.resolve():
                pdf_file.rename(dest_in_account)

            if not is_duplicate:
                renamed_count += 1
        except Exception as e:
            print(f"\n[ERROR] Renaming {pdf_file.name}: {e}")
            skipped_count += 1

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"PDF's Found:          {len(pdf_files)}")
    print(f"Total PDFs processed: {len(pdf_files)}")
    print(f"Duplicates moved:     {duplicates_moved}")
    print(f"Files renamed:        {renamed_count}")
    print(f"Files skipped:        {skipped_count}")
    # print(f"\nDuplicate folder:\n  {duplicate_dir}")
    print("=" * 70)


# ── Step 3: Move renamed bills into the FY archive ───────────────────────

def get_fiscal_year(year: int, month: int) -> str:
    """
    Hawaii fiscal year logic (matches all three original scripts):
      - Jul-Dec => next fiscal year
      - Jan-Jun => same fiscal year
    """
    fy = year + 1 if month >= 7 else year
    return f"FY{str(fy)[-2:]}"


def move_bills_to_archive(source_dir, archive_dir, filename_pattern):
    """
    Move canonically-named bills out of input_bills into
    bill_archive/FY<YY>/<account>/, based on the account and date encoded
    in the filename.

    NOTE: Files are placed in an account subfolder within the FY folder
    (e.g. bill_archive/FY25/2015984/).

    filename_pattern: compiled regex with named groups acct, year, month, day
                       e.g. re.compile(r"^(?P<acct>\\d+)_(?P<year>\\d{4})_(?P<month>\\d{2})_(?P<day>\\d{2}).*\\.pdf$", re.IGNORECASE)
    """
    source_dir = Path(source_dir)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped = 0

    # Determine the duplicate folder path used by the gas pipeline.
    duplicate_dir = source_dir / "duplicate_bills"
    duplicate_dir_resolved = str(Path(duplicate_dir).resolve())

    # The input folder can contain account subfolders after renaming.
    for root, _, files in os.walk(source_dir):
        for file in files:
            if file.lower().endswith(".pdf"):
                file_path = Path(root) / file
                # Skip any files that live in the duplicate_bills folder.
                if str(Path(root).resolve()).startswith(duplicate_dir_resolved):
                    # leave duplicates alone — they are handled elsewhere
                    skipped += 1
                    continue
                match = filename_pattern.match(file)

                if not match:
                    print(f"Skipping unmatched filename: {file}")
                    skipped += 1
                    continue

                year = int(match.group("year"))
                month = int(match.group("month"))

                fy_folder = get_fiscal_year(year, month)

                # Place files in FY folder / account subfolder
                target_dir = archive_dir / fy_folder / match.group("acct")
                target_dir.mkdir(parents=True, exist_ok=True)

                destination = target_dir / file

                # avoid overwrite collisions
                if destination.exists():
                    base = file_path.stem
                    ext = file_path.suffix
                    counter = 1

                    while destination.exists():
                        destination = target_dir / f"{base}_{counter}{ext}"
                        counter += 1
                
                # if you want to see the file names that are being moved
                # print(f"Moving {file} -> bill_archive/{fy_folder}/{yymm}")
                
                shutil.move(str(file_path), str(destination))

                moved += 1

    print(f"Finished moving bills. Total moved: {moved}, Total skipped: {skipped}")

    # delete empty subfolders from input folder
    subfolder = 0
    for root, dirs, _ in os.walk(source_dir):
        for dir in dirs:
            dir_path = Path(root) / dir
            # never remove the duplicate folder (or its subfolders)
            try:
                if str(dir_path.resolve()).startswith(duplicate_dir_resolved):
                    continue
            except Exception:
                # on resolution issues, skip removal to be safe
                continue
            if not any(dir_path.iterdir()):
                # print(f"Removing empty folder: {dir_path}")
                dir_path.rmdir()
                subfolder += 1

    print(f"Empty subfolders removed: {subfolder}")

# ── Step 3.5 ─────────────────────────────────
# Backfill duplicate_bills into the archive when the DB
# already has the data (e.g. bills uploaded before this
# archiving system existed) 

def archive_duplicates_already_in_database(
    duplicate_dir,
    archive_dir,
    filename_pattern,
    filename_suffix,
    get_account_and_date_to,
    database="uhm2023",
    table="gas.sng_bills",
):
    """
    Check every PDF sitting in duplicate_dir to see whether its bill is
    already in `table` (matched on account + date_to) -- and if so, move
    it into bill_archive/FY<YY>/ instead of leaving it stranded in
    duplicate_bills/ forever.
 
    This is for backfilling: bills whose data was uploaded to the database
    before this archiving system existed can just be dropped into
    duplicate_bills/, and this function will confirm each one's data is
    really in the database and file it into the archive -- without
    re-running it through extract -> upload (which would try to insert
    data that's already there).
 
    A bill with NO matching database row is left untouched in
    duplicate_bills/ -- moving it would risk losing a bill whose data was
    never actually captured, so this only ever archives what the database
    already confirms.
 
    duplicate_dir:    the duplicate_bills folder to scan (flat, not recursive)
    archive_dir:      bill_archive/ root
    filename_pattern: compiled regex with named groups acct, year, month,
                      day -- same one move_bills_to_archive() uses -- so a
                      file that's already canonically named can be read
                      straight off its filename, no PDF parsing needed
    filename_suffix:  suffix used when a file needs to be renamed into
                      canonical form, e.g. "gas_bill"
    get_account_and_date_to: a function(pdf_path) -> (account, date_to) as
                      (str, datetime.date), or (None, None) if it can't be
                      read. Fallback for files still under their original
                      (non-canonical) name -- project-specific, same idea
                      as organize_bills()'s extract_bill_info.
    """
    duplicate_dir = Path(duplicate_dir)
    archive_dir = Path(archive_dir)
 
    if not duplicate_dir.exists():
        print(f"[ERROR] Folder does not exist:\n  {duplicate_dir}")
        return
 
    pdf_files = [f for f in duplicate_dir.glob("*.pdf") if f.parent == duplicate_dir]
    if not pdf_files:
        print("No PDF files found in duplicate_bills/.")
        return
 
    print(f"Checking {len(pdf_files)} PDF(s) in duplicate_bills/ against {table}...")
 
    archived = 0
    left_alone = 0
    skipped = 0
 
    for pdf_file in pdf_files:
        match = filename_pattern.match(pdf_file.name)
 
        if match:
            account = match.group("acct")
            date_to = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        else:
            account, date_to = get_account_and_date_to(pdf_file)
            if not account or not date_to:
                print(f"  [WARNING] Could not determine account/date for {pdf_file.name}. Skipping.")
                skipped += 1
                continue
 
        date_to_str = date_to.strftime("%Y-%m-%d") if isinstance(date_to, (datetime, date)) else str(date_to)
 
        query = (
            f"SELECT 1 AS found FROM {table} "
            f"WHERE account = '{account}' AND date_to = '{date_to_str}' LIMIT 1"
        )
        check_csv = Path(tempfile.gettempdir()) / f"gas_archive_check_{account}_{date_to_str}.csv"
        pulled = extract_query_to_csv(database, query, check_csv)
 
        found = False
        if pulled and check_csv.exists():
            with open(check_csv, newline="") as f:
                reader = csv.reader(f)
                next(reader, None)  # header row
                found = next(reader, None) is not None
        if check_csv.exists():
            check_csv.unlink()
 
        if not found:
            print(
                f"  [INFO] No matching row in {table} for account {account}, "
                f"date_to {date_to_str} -- leaving {pdf_file.name} in duplicate_bills/."
            )
            left_alone += 1
            continue
 
        # Confirmed in the database -- safe to archive. Rename into
        # canonical form first if it isn't already.
        canonical_name = f"{account}_{date_to.strftime('%Y_%m_%d')}_{get_filename_suffix(account, filename_suffix)}.pdf"
        source = pdf_file
        if pdf_file.name != canonical_name:
            renamed = pdf_file.parent / canonical_name
            counter = 1
            while renamed.exists():
                renamed = pdf_file.parent / f"{Path(canonical_name).stem}_{counter}.pdf"
                counter += 1
            pdf_file.rename(renamed)
            source = renamed
 
        fy_folder = get_fiscal_year(date_to.year, date_to.month)
        target_dir = archive_dir / fy_folder / str(account)
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / source.name
 
        counter = 1
        while destination.exists():
            destination = target_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
 
        shutil.move(str(source), str(destination))
        print(f"  [OK] {source.name} confirmed in {table} -- archived to {fy_folder}/{account}/")
        archived += 1
 
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"PDFs checked:                          {len(pdf_files)}")
    print(f"Archived:                              {archived}")
    print(f"Left in duplicate_bills/ (no DB match): {left_alone}")
    print(f"Skipped (couldn't read account/date):   {skipped}")
    print("=" * 70)

# ── Step 4: Upload CSV to database ───────────────────────────────────────

def add_upload_timestamp(csv_path):
    """
    Stamp every row in the CSV with the current date and time in its
    upload_time column, overwriting the file in place. Called right before
    the CSV gets \\copy'd into the database, so upload_time reflects when
    the upload actually happened -- not when the PDF was extracted.
    """
    csv_path = Path(csv_path)
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if not fieldnames or "upload_time" not in fieldnames:
        print(f"[WARNING] {csv_path.name} has no upload_time column -- nothing to stamp.")
        return

    for row in rows:
        row["upload_time"] = upload_time

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OK] Stamped {len(rows)} row(s) with upload_time = {upload_time}")


def upload_csv_to_database():

    # 1. Get the absolute path of the folder where this script lives
    # (/home/campusenergy/shared/gas_bills/scipts)
    script_dir = Path(__file__).resolve()
    
    # 2. Look in 'output/' for the most recent CSV matching our pattern.
    output_dir = script_dir.parent / "output"

    # Pick the newest CSV matching our pattern in the output directory.
    candidates = sorted(
        [p for p in output_dir.glob("gas_bills_extracted*.csv") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        print(f"[ERROR] No CSV matching 'gas_bills_extracted*.csv' found in: {output_dir}")
        return False

    csv_path = candidates[0]
    print(f"[INFO] Using most recent CSV for upload: {csv_path}")

    # Stamp every row with the current date/time before it gets uploaded.
    add_upload_timestamp(csv_path)

    # Optionally keep only the 5 most recent CSVs to avoid accumulating old files.
    try:
        base_stem = "gas_bills_extracted"
        backups = sorted(
            [p for p in output_dir.glob(f"{base_stem}_*{csv_path.suffix}") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[5:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] Could not clean up old CSVs: {e}")

    # Empty the staging table before uploading the current extract.
    empty_command = [
        "psql",
        "uhm2023",
        "-c",
        r"""TRUNCATE TABLE gas.sng_bills_staging;"""
    ]

    # Copy data to the staging table.
    copy_command = [
        "psql",
        "uhm2023",
        "-c",
        rf"""\copy gas.sng_bills_staging(account, date_from, date_to, therms, therms_adjusted, "billed_$", note, upload_time) FROM '{csv_path}' CSV HEADER;"""
    ]

    # Insert new rows into the main table without duplicating existing rows.
    insert_command = [
        "psql",
        "uhm2023",
        "-c",
        rf"""
        INSERT INTO gas.sng_bills
            SELECT * FROM gas.sng_bills_staging
            ON CONFLICT DO NOTHING;
        """
    ]

    try: 
        subprocess.run(empty_command, check=True)
        subprocess.run(copy_command, check=True)
        subprocess.run(insert_command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database upload failed (psql exit code {e.returncode}).")
        print(f"        {csv_path} was NOT fully uploaded to gas.sng_bills.")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False

    print(f"[OK] Uploaded {csv_path} to gas.sng_bills")
    return True

def extract_query_to_csv(database, query, csv_path):
    """
    Run, via psql:
        \\copy (<query>) TO '<csv_path>' CSV HEADER;

    Inverse of upload_csv_to_database() — pulls a query's results down
    into a CSV instead of loading a CSV into a table. Used for pre-processing
    steps that need to see what's already in the database (e.g. the last
    billed date per account) before processing new bills.

    Only works where psql can reach the database (the server, not a laptop).

    database: e.g. "uhm2023"
    query:    e.g. "SELECT account, MAX(date_to) AS max FROM gas.sng_bills GROUP BY account"
    csv_path: where to write the CSV, e.g. config.LAST_DATES_CSV

    Returns True if the export succeeded, False if it failed.
    The query is what it used to be it's different in config.py so that it catches missing or skipped months
    """
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "psql",
        database,
        "-c",
        rf"""\copy ({query}) TO '{csv_path}' CSV HEADER;""",
    ]

    try:
        subprocess.run(command, check=True)
        print(f"[OK] Extracted query results to {csv_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database extract failed (psql exit code {e.returncode}).")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False


def upload_and_archive(source_dir, archive_dir, filename_pattern):
    """
    Upload the CSV to the database, and ONLY IF that succeeds, move the
    corresponding bills from source_dir into the FY archive.
 
    This exists so a failed database upload can never silently leave you
    with a bill sitting in bill_archive/ whose data never made it into
    the database.
    """
    success = upload_csv_to_database()

    # comment this if statement out if you want to test the archive step even if the upload fails
    if not success:
        print("\nSkipping archive step - bills remain in:")
        print(f"  {source_dir}")
        print("Fix the upload issue, then re-run this script.")
        return False
 
    print("\nUpload succeeded — proceeding to archive bills.")
    move_bills_to_archive(source_dir, archive_dir, filename_pattern)
    return True

# Specific functions for gas bill extraction
def clean_dollar_amount(amount_str):
    """Clean dollar amount strings and convert to float."""
    amount_str = amount_str.replace(",", "").strip()
    if amount_str.startswith("-"):
        return -float(amount_str[1:])
    return float(amount_str)


# LPG functions
# ─────────────────────────────────────────────────────────────────────────
# ADD to the bottom of gas_common.py (uses the imports and helpers already
# in that file: csv, os, shutil, subprocess, tempfile, Path, date, datetime,
# add_upload_timestamp, extract_query_to_csv, get_fiscal_year,
# move_bills_to_archive, LPG_SUFFIX)
# ─────────────────────────────────────────────────────────────────────────


# ── LPG: Upload CSV to database ──────────────────────────────────────────

def upload_lpg_csv_to_database():
    """
    LPG version of upload_csv_to_database(): loads the newest
    lpg_bills_extracted*.csv from output/ into gas.lpg_bills_staging, then
    inserts new rows into gas.lpg_bills (existing rows are never overwritten).

    The column list is read from the CSV header, so it always matches
    whatever the LPG extract script wrote (e.g. account, date, gallons,
    billed_$ [, upload_time]).
    """
    output_dir = Path(__file__).resolve().parent / "output"

    candidates = sorted(
        [p for p in output_dir.glob("lpg_bills_extracted*.csv") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        print(f"[ERROR] No CSV matching 'lpg_bills_extracted*.csv' found in: {output_dir}")
        return False

    csv_path = candidates[0]
    print(f"[INFO] Using most recent LPG CSV for upload: {csv_path}")

    # Keep only the 5 most recent LPG CSVs
    try:
        backups = sorted(
            [p for p in output_dir.glob(f"lpg_bills_extracted_*{csv_path.suffix}") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[5:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] Could not clean up old LPG CSVs: {e}")

    # Build the column list from the CSV header (quote names like billed_$)
    with open(csv_path, newline="") as f:
        header = next(csv.reader(f), [])
    columns = ", ".join(f'"{c}"' for c in header)

    empty_command = [
        "psql", "uhm2023", "-c",
        r"""TRUNCATE TABLE gas.lpg_bills_staging;""",
    ]
    copy_command = [
        "psql", "uhm2023", "-c",
        rf"""\copy gas.lpg_bills_staging({columns}) FROM '{csv_path}' CSV HEADER;""",
    ]
    insert_command = [
        "psql", "uhm2023", "-c",
        rf"""
        INSERT INTO gas.lpg_bills ({columns})
            SELECT {columns} FROM gas.lpg_bills_staging
            ON CONFLICT DO NOTHING;
        """,
    ]

    try:
        print("[1/3] Emptying gas.lpg_bills_staging ...")
        subprocess.run(empty_command, check=True)
        print("[2/3] Copying CSV into gas.lpg_bills_staging ...")
        subprocess.run(copy_command, check=True)
        print("[3/3] Inserting new rows into gas.lpg_bills ...")
        subprocess.run(insert_command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database upload failed (psql exit code {e.returncode}).")
        print(f"        {csv_path} was NOT fully uploaded to gas.lpg_bills.")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False

    print(f"[OK] Uploaded {csv_path} to gas.lpg_bills")
    return True


# ── LPG: Upload + archive ────────────────────────────────────────────────

def upload_and_archive_lpg(source_dir, archive_dir, filename_pattern):
    """
    LPG version of upload_and_archive(): upload the LPG CSV to
    gas.lpg_bills, and ONLY IF that succeeds, move the bills from
    source_dir into bill_archive/FY<YY>/<account>/.
    """
    success = upload_lpg_csv_to_database()

    if not success:
        print("\nSkipping archive step - bills remain in:")
        print(f"  {source_dir}")
        print("Fix the upload issue, then re-run this script.")
        return False

    print("\nUpload succeeded — proceeding to archive bills.")
    move_bills_to_archive(source_dir, archive_dir, filename_pattern)
    return True


# ── LPG: Backfill duplicate_bills into the archive ───────────────────────

def archive_duplicates_already_in_database_lpg(
    duplicate_dir,
    archive_dir,
    filename_pattern,
    get_account_and_date,
    filename_suffix=None,
    database="uhm2023",
    table="gas.lpg_bills",
):
    """
    LPG version of archive_duplicates_already_in_database().

    Checks every PDF in duplicate_dir against gas.lpg_bills (matched on
    account + date, where date is the bill date) and, if the row is there,
    renames the PDF to ACCOUNT_YYYY_MM_DD_lpg_bill.pdf and moves it into
    bill_archive/FY<YY>/<account>/. A bill with no matching row is left in
    duplicate_bills/ untouched.

    duplicate_dir:        the duplicate_bills folder to scan (flat)
    archive_dir:          bill_archive/ root
    filename_pattern:     compiled regex with named groups acct, year,
                          month, day (same one move_bills_to_archive() uses)
    get_account_and_date: function(pdf_path) -> (account, bill_date), or
                          (None, None). Fallback for files that aren't
                          canonically named yet.
    filename_suffix:      defaults to LPG_SUFFIX ("lpg_bill")
    """
    filename_suffix = filename_suffix or LPG_SUFFIX
    duplicate_dir = Path(duplicate_dir)
    archive_dir = Path(archive_dir)

    if not duplicate_dir.exists():
        print(f"[ERROR] Folder does not exist:\n  {duplicate_dir}")
        return

    pdf_files = [f for f in duplicate_dir.glob("*.pdf") if f.parent == duplicate_dir]
    if not pdf_files:
        print("No PDF files found in duplicate_bills/.")
        return

    print(f"Checking {len(pdf_files)} PDF(s) in duplicate_bills/ against {table}...")

    archived = 0
    left_alone = 0
    skipped = 0

    for i, pdf_file in enumerate(pdf_files, 1):
        print(f"[{i}/{len(pdf_files)}] {pdf_file.name}")
        match = filename_pattern.match(pdf_file.name)

        if match:
            account = match.group("acct")
            bill_date = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        else:
            account, bill_date = get_account_and_date(pdf_file)
            if not account or not bill_date:
                print(f"  [WARNING] Could not determine account/date for {pdf_file.name}. Skipping.")
                skipped += 1
                continue

        date_str = bill_date.strftime("%Y-%m-%d") if isinstance(bill_date, (datetime, date)) else str(bill_date)

        query = (
            f"SELECT 1 AS found FROM {table} "
            f"WHERE account = '{account}' AND date = '{date_str}' LIMIT 1"
        )
        check_csv = Path(tempfile.gettempdir()) / f"lpg_archive_check_{account}_{date_str}.csv"
        pulled = extract_query_to_csv(database, query, check_csv)

        found = False
        if pulled and check_csv.exists():
            with open(check_csv, newline="") as f:
                reader = csv.reader(f)
                next(reader, None)  # header row
                found = next(reader, None) is not None
        if check_csv.exists():
            check_csv.unlink()

        if not found:
            print(
                f"  [INFO] No matching row in {table} for account {account}, "
                f"date {date_str} -- leaving {pdf_file.name} in duplicate_bills/."
            )
            left_alone += 1
            continue

        # Confirmed in the database -- rename to canonical form, then archive
        canonical_name = f"{account}_{bill_date.strftime('%Y_%m_%d')}_{filename_suffix}.pdf"
        source = pdf_file
        if pdf_file.name != canonical_name:
            renamed = pdf_file.parent / canonical_name
            counter = 1
            while renamed.exists():
                renamed = pdf_file.parent / f"{Path(canonical_name).stem}_{counter}.pdf"
                counter += 1
            pdf_file.rename(renamed)
            source = renamed

        fy_folder = get_fiscal_year(bill_date.year, bill_date.month)
        target_dir = archive_dir / fy_folder / str(account)
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / source.name

        counter = 1
        while destination.exists():
            destination = target_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1

        shutil.move(str(source), str(destination))
        print(f"  [OK] {source.name} confirmed in {table} -- archived to {fy_folder}/{account}/")
        archived += 1

    print("\n" + "=" * 70)
    print("SUMMARY (LPG)")
    print("=" * 70)
    print(f"PDFs checked:                          {len(pdf_files)}")
    print(f"Archived:                              {archived}")
    print(f"Left in duplicate_bills/ (no DB match): {left_alone}")
    print(f"Skipped (couldn't read account/date):   {skipped}")
    print("=" * 70)