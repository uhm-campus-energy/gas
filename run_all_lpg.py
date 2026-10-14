"""
Run all LPG pipeline scripts in order.

Script 0) is shared with the natural gas pipeline and lives in scripts/;
scripts 1)-3) live in scripts/lpg/.

Usage:
    python run_all_lpg.py [--continue-on-error]

By default the runner stops on the first failing script. Use
`--continue-on-error` to run them all regardless of individual failures.
As the data won't be uploaded or moved to archive if something goes wrong.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Paths relative to the scripts directory (gas_bills/scripts).
SCRIPTS = [
    "lpg/0) pre-process, find the last dates in database.py",
    "lpg/1) lpg_organize_bills.py",
    "lpg/2) lpg_extract.py",
    "lpg/3) lpg_upload_archive.py",
]


def find_scripts_dir(override: str | None) -> Path:
    """Return the directory that contains script 0) and the lpg/ subfolder.

    If `override` is provided, use that. Otherwise try several sensible
    locations and pick the first one that contains at least one of the
    expected scripts.
    """
    candidates = []
    if override:
        candidates.append(Path(override).expanduser().resolve())

    file_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()
    candidates.extend([
        file_dir / "scripts",
        file_dir,
        cwd / "scripts",
        cwd,
        file_dir.parent / "scripts",
    ])

    for cand in candidates:
        try:
            if cand.is_dir() and any((cand / name).exists() for name in SCRIPTS):
                return cand
        except Exception:
            continue

    # fallback to the scripts folder next to this file
    return file_dir / "scripts"


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

        proc = subprocess.run([sys.executable, str(script_path)], cwd=str(script_path.parent), capture_output=True, text=True)

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
    parser = argparse.ArgumentParser(description="Run the LPG scripts in order.")
    parser.add_argument("--continue-on-error", action="store_true", help="continue running remaining scripts when one fails")
    parser.add_argument("--scripts-dir", help="path to gas_bills/scripts (overrides automatic detection)")
    args = parser.parse_args()

    code = run_scripts(args.continue_on_error, args.scripts_dir)
    sys.exit(code)
