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


def is_synthetic(path: Path) -> bool:
    """Files written by extend_ledger_from_transactions.py contain
    pseudo-location 'EU' rows; real Detailed-view exports never do."""
    loc = pd.read_csv(path, usecols=["Location"], dtype=str)["Location"]
    return bool((loc == "EU").any())


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
    if dates.isna().mean() > 0.02:
        sys.exit("Ledger dates unparseable (expected MM/DD/YYYY) — was the "
                 "export downloaded in a different locale?")
    stamp = dates.max().strftime("%Y-%m-%d")

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    dest = LEDGER_DIR / f"inventory_ledger_{stamp}.csv.gz"
    with open(src_path, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"Wrote {dest.name} ({size_mb:.1f} MB), covering through {stamp}.")

    # A real import supersedes any synthesized roll-forward tails — remove
    # them even when they carry a LATER date stamp, otherwise the dashboard's
    # newest-file pick would silently keep using the synthesized data and this
    # import would never take effect. (Re-run extend_ledger_from_transactions.py
    # afterwards if you want a fresh tail on top of the new anchor.)
    for p in sorted(LEDGER_DIR.glob("inventory_ledger_*.csv.gz")):
        if p != dest and is_synthetic(p):
            p.unlink()
            print(f"Removed synthesized snapshot {p.name} (superseded by this "
                  "real import).")

    # Prune old snapshots — but never the newest real one.
    snaps = sorted(LEDGER_DIR.glob("inventory_ledger_*.csv.gz"))
    real = [p for p in snaps if not is_synthetic(p)]
    keep = set(snaps[-KEEP_LATEST:]) | ({real[-1]} if real else set())
    for old in snaps:
        if old not in keep:
            old.unlink()
            print(f"Pruned old snapshot: {old.name}")

    print("Done. Commit & push amazon_ledger/ so the dashboard picks it up.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python add_ledger.py /path/to/inventory-ledger.csv")
    main(sys.argv[1])
