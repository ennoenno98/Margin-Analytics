"""Add an Amazon FBA Inventory Ledger export to the OOS dashboard.

The OOS Impact dashboard (`oos_analytics.py`) reads the newest
`amazon_ledger/inventory_ledger_*.csv.gz`. This helper takes a freshly
downloaded ledger CSV (Seller Central → Reports → Fulfilment → Inventory
Ledger → "Detailed View", date range = trailing 12 months), gzips it and
date-stamps it by the latest date in the file so the dashboard picks it up.

Usage:
    python add_ledger.py /path/to/inventory-ledger.csv
    # then commit:  git add amazon_ledger && git commit -m "data: ledger refresh" && git push
"""
from __future__ import annotations

import gzip
import shutil
import sys
from pathlib import Path

import pandas as pd

LEDGER_DIR = Path(__file__).resolve().parent / "amazon_ledger"
KEEP_LATEST = 4  # keep the N most recent snapshots, prune older ones


def main(src: str) -> None:
    src_path = Path(src)
    if not src_path.exists():
        sys.exit(f"File not found: {src_path}")

    # Read just the Date column to derive the stamp; validate it's a ledger.
    head = pd.read_csv(src_path, nrows=5)
    if "Date" not in head.columns or "MSKU" not in head.columns:
        sys.exit("This does not look like an FBA Inventory Ledger export "
                 "(missing 'Date' / 'MSKU' columns).")
    dates = pd.to_datetime(
        pd.read_csv(src_path, usecols=["Date"])["Date"],
        format="%m/%d/%Y", errors="coerce",
    )
    stamp = dates.max().strftime("%Y-%m-%d")

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    dest = LEDGER_DIR / f"inventory_ledger_{stamp}.csv.gz"
    with open(src_path, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"Wrote {dest.name} ({size_mb:.1f} MB), covering through {stamp}.")

    # Prune old snapshots.
    snaps = sorted(LEDGER_DIR.glob("inventory_ledger_*.csv.gz"))
    for old in snaps[:-KEEP_LATEST]:
        old.unlink()
        print(f"Pruned old snapshot: {old.name}")

    print("Done. Commit & push amazon_ledger/ so the dashboard picks it up.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python add_ledger.py /path/to/inventory-ledger.csv")
    main(sys.argv[1])
