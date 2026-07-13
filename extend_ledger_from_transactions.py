"""Extend the Amazon Inventory Ledger tail from a Transaction-view export.

The OOS dashboard reads the Inventory Ledger "Detailed view" (daily balances).
Seller Central's "Transaction view" (event-level) can't replace it — it has no
balance columns — but it CAN roll the known balances forward: starting from the
newest day in the existing ledger, apply each day's net SELLABLE events
(excluding warehouse transfers, which don't change network availability) per
SKU x region (GB vs EU) and synthesize Detailed-view-shaped rows for the tail.

Validated against the real ledger over an overlap window: median error 0 units,
~94% of SKU-regions within +/-5 units.

Usage:
    python extend_ledger_from_transactions.py /path/to/transaction-view.txt
    # then: git add amazon_ledger && git commit && git push
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

import numpy as np
import pandas as pd

LEDGER_DIR = Path(__file__).resolve().parent / "amazon_ledger"
KEEP_LATEST = 4


def is_synthetic(path: Path) -> bool:
    """Files written by this script contain pseudo-location 'EU' rows; real
    Detailed-view exports never do."""
    loc = pd.read_csv(path, usecols=["Location"], dtype=str)["Location"]
    return bool((loc == "EU").any())


def latest_ledger() -> Path:
    cands = sorted(LEDGER_DIR.glob("inventory_ledger_*.csv.gz"))
    if not cands:
        sys.exit("No existing ledger in amazon_ledger/ — need a Detailed-view "
                 "baseline to roll forward from.")
    return cands[-1]


def main(tx_path: str) -> None:
    old_path = latest_ledger()
    led = pd.read_csv(old_path, dtype=str)
    led["_d"] = pd.to_datetime(led["Date"], format="%m/%d/%Y", errors="coerce")
    base_day = led["_d"].max()

    # Transaction-view exports come both tab-separated (.txt) and
    # comma-separated (.csv) depending on how they were downloaded — sniff it.
    with open(tx_path, "r", encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    sep = "\t" if first.count("\t") >= first.count(",") else ","
    tx = pd.read_csv(tx_path, sep=sep, dtype=str)
    if "Event Type" not in tx.columns or "Quantity" not in tx.columns:
        sys.exit("This does not look like a Transaction-view export "
                 "(missing 'Event Type' / 'Quantity').")
    tx["Date"] = pd.to_datetime(tx["Date"], format="%m/%d/%Y", errors="coerce")
    if tx["Date"].isna().mean() > 0.02:
        sys.exit("Transaction dates unparseable (expected MM/DD/YYYY) — was the "
                 "export downloaded in a different locale?")
    tx["Quantity"] = pd.to_numeric(tx["Quantity"], errors="coerce").fillna(0)
    tx = tx[(tx["Disposition"] == "SELLABLE") & (tx["Date"] > base_day)]
    if tx.empty:
        sys.exit(f"Transaction file has no SELLABLE events after {base_day.date()} "
                 "— nothing to extend.")
    tx["region"] = np.where(tx["Country"] == "GB", "GB", "EU")
    end_day = tx["Date"].max()

    # Baseline: available (= on-hand + in-transit) per SKU x region on base_day.
    lb = led[(led["_d"] == base_day) & (led["Disposition"] == "SELLABLE")].copy()
    for c in ("Ending Warehouse Balance", "In Transit Between Warehouses"):
        lb[c] = pd.to_numeric(lb[c], errors="coerce").fillna(0)
    lb["region"] = np.where(lb["Location"] == "GB", "GB", "EU")
    lb["av"] = lb["Ending Warehouse Balance"] + lb["In Transit Between Warehouses"]
    base = lb.groupby(["MSKU", "region"])["av"].sum()

    # Daily deltas: net of all non-transfer events (transfers conserve network
    # availability = on-hand + in-transit, which is what the dashboard uses).
    ev = tx[tx["Event Type"] != "WhseTransfers"]
    net = ev.groupby(["MSKU", "region", "Date"])["Quantity"].sum()
    ship = (tx[tx["Event Type"] == "Shipments"]
            .groupby(["MSKU", "region", "Date"])["Quantity"].sum())
    rcpt = (tx[tx["Event Type"] == "Receipts"]
            .groupby(["MSKU", "region", "Date"])["Quantity"].sum())

    days = pd.date_range(base_day + pd.Timedelta(days=1), end_day, freq="D")
    keys = sorted(set(base.index) | set(net.index.droplevel("Date").unique()))
    idx = pd.MultiIndex.from_tuples(
        [(s, r, d) for (s, r) in keys for d in days],
        names=["MSKU", "region", "Date"])
    panel = pd.DataFrame(index=idx)
    panel["net"] = net.reindex(idx).fillna(0.0)
    panel["ship"] = ship.reindex(idx).fillna(0.0)
    panel["rcpt"] = rcpt.reindex(idx).fillna(0.0)
    panel = panel.reset_index()
    panel["bal"] = (panel.groupby(["MSKU", "region"])["net"].cumsum()
                    + panel.set_index(["MSKU", "region"]).index.map(base).fillna(0).values)

    # Synthesize Detailed-view rows (one pseudo-location per region; balance
    # already includes in-transit, so the In-Transit column is 0).
    cols = list(led.drop(columns=["_d"]).columns)
    out = pd.DataFrame({c: "" for c in cols}, index=panel.index)
    out["Date"] = panel["Date"].dt.strftime("%m/%d/%Y")
    out["MSKU"] = panel["MSKU"]
    out["Disposition"] = "SELLABLE"
    out["Location"] = panel["region"]          # "EU" pseudo-location / "GB"
    out["Ending Warehouse Balance"] = (panel["bal"].clip(lower=0)
                                       .round(0).astype(int).astype(str))
    out["In Transit Between Warehouses"] = "0"
    out["Customer Shipments"] = panel["ship"].round(0).astype(int).astype(str)
    out["Receipts"] = panel["rcpt"].round(0).astype(int).astype(str)

    merged = pd.concat([led.drop(columns=["_d"]), out[cols]], ignore_index=True)
    dest = LEDGER_DIR / f"inventory_ledger_{end_day.date()}.csv.gz"
    with gzip.open(dest, "wt", compresslevel=6, newline="") as f:
        merged.to_csv(f, index=False)
    print(f"Extended {old_path.name} ({base_day.date()}) -> {dest.name} "
          f"({end_day.date()}), +{len(out):,} synthesized rows, "
          f"{panel['MSKU'].nunique()} SKUs.")

    snaps = sorted(LEDGER_DIR.glob("inventory_ledger_*.csv.gz"))
    real = [p for p in snaps if not is_synthetic(p)]
    # Never prune the newest REAL Detailed-view snapshot — it is the only
    # anchor the roll-forward chain can be re-derived from.
    keep = set(snaps[-KEEP_LATEST:]) | ({real[-1]} if real else set())
    for old in snaps:
        if old not in keep:
            old.unlink()
            print(f"Pruned old snapshot: {old.name}")
    print("Done. Commit & push amazon_ledger/ so the dashboard picks it up.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python extend_ledger_from_transactions.py <transaction-view.txt>")
    main(sys.argv[1])
