"""
Run all gas_bills pipeline scripts in order.

Usage:
    python run_all.py [--continue-on-error]

By default the runner stops on the first failing script. Use
`--continue-on-error` to run them all regardless of individual failures. 
As the data won't be uploaded or moved to archive if something goes wrong.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = [
    "0) pre-process, find the last dates in database.py",
    "1) organize_gas_bills.py",
    "2) gas_bill_extract.py",
    "3) upload_and_archive_data.py",
]


def find_scripts_dir(override: str | None) -> Path:
    """Return the directory that contains the pipeline scripts.

    If `override` is provided, use that. Otherwise try several sensible
    locations (the directory containing this file, the current working
    directory, and common "scripts" subfolders) and pick the first one
    that exists and contains at least one of the expected script names.
    """
    candidates = []
    if override:
        candidates.append(Path(override).expanduser().resolve())

    file_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()
    candidates.extend([
        file_dir,
        cwd,
        cwd / "scripts",
        cwd / "scipts",
        file_dir / "scripts",
        file_dir / "scipts",
        file_dir.parent / "scripts",
        file_dir.parent / "scipts",
    ])

    for cand in candidates:
        try:
            if cand.exists() and cand.is_dir():
                # check for at least one script file present
                for name in SCRIPTS:
                    if (cand / name).exists():
                        return cand
        except Exception:
            continue

    # fallback to the directory containing this file
    return file_dir


def run_scripts(continue_on_error: bool, scripts_dir_override: str | None = None) -> int:
    scripts_dir = find_scripts_dir(scripts_dir_override)
    print(f"Using scripts directory: {scripts_dir}")
    exit_code = 0

    for name in SCRIPTS:
        script_path = scripts_dir / name
        print(f"\n--- Running: {name} ---")
        if not script_path.exists():
            print(f"[ERROR] Script not found: {script_path}")
            exit_code = 2
            if not continue_on_error:
                break
            else:
                continue

        proc = subprocess.run([sys.executable, str(script_path)], cwd=str(scripts_dir), capture_output=True, text=True)

        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)

        if proc.returncode != 0:
            print(f"[ERROR] Script exited with code {proc.returncode}: {name}")
            exit_code = proc.returncode or 1
            if not continue_on_error:
                break

    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the gas_bills scripts in order.")
    parser.add_argument("--continue-on-error", action="store_true", help="continue running remaining scripts when one fails")
    parser.add_argument("--scripts-dir", help="path to the folder containing the pipeline scripts (overrides automatic detection)")
    args = parser.parse_args()

    code = run_scripts(args.continue_on_error, args.scripts_dir)
    sys.exit(code)
