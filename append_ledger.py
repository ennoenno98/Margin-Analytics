"""Append a new Amazon FBA Inventory Ledger export onto the full-history ledger.

This is the standard way to refresh stock data. Seller Central exports of the
Inventory Ledger "Detailed View" cover only a short window, but the dashboard
needs year-long stock coverage. This script:

1. reads the export (.csv or .txt, comma- or tab-separated),
2. maps each Location to the EU pool or the GB warehouse (country codes:
   GB -> GB; fulfilment-centre codes: UK FCs -> GB, everything else -> EU),
3. aggregates SELLABLE stock per SKU x region x day,
4. checks the overlap days against the existing ledger (EU/GB balances), and
5. appends only the days after the current ledger's last day, writing
   amazon_ledger/inventory_ledger_<last day>.csv.gz and removing the old file.

Usage:
    python append_ledger.py /path/to/inventory-ledger-export.txt
    # then: git add amazon_ledger && git commit -m "data: ledger refresh" && git push

(add_ledger.py replaces the ledger with the export alone — use it only for a
full-history export.)
"""
from __future__ import annotations

import glob
import gzip
import os
import sys

import numpy as np
import pandas as pd

LEDGER_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "amazon_ledger", "inventory_ledger_*.csv.gz")
# UK fulfilment centres (validated against country-code exports to <0.1 %).
UK_FC = {"BHX2", "BHX3", "BHX4", "BRS1", "BRS2", "CWL1", "DSA2", "DSA6", "EDI4",
         "EMA1", "EMA2", "EMA4", "EUK5", "LBA2", "LBA4", "LBA5", "LCY2", "LCY3",
         "LTN1", "LTN4", "MAN1", "MAN2", "MAN3", "MAN4", "MME1", "MME2", "NCL1",
         "NCL2", "STN6", "UKK1", "XBH6", "XBH7", "XHH1", "XLB5", "XLH3", "XLH4"}
UK_PREFIX = {"BHX", "BRS", "CWL", "DSA", "EDI", "EMA", "EUK", "GLA", "LBA", "LCY",
             "LTN", "MAN", "MME", "NCL", "STN"}
COUNTRY_CODES = {"DE", "FR", "IT", "ES", "GB", "IE", "CZ", "PL", "SK", "NL", "BE", "SE", "AT"}
NUM = ["Ending Warehouse Balance", "In Transit Between Warehouses",
       "Customer Shipments", "Receipts"]
ZERO_COLS = ["Starting Warehouse Balance", "Customer Returns", "Vendor Returns",
             "Warehouse Transfer In/Out", "Found", "Lost", "Damaged", "Disposed",
             "Other Events", "Unknown Events"]


def read_export(path: str) -> pd.DataFrame:
    with open(path, encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    sep = "\t" if first.count("\t") >= first.count(",") else ","
    df = pd.read_csv(path, sep=sep, dtype=str)
    missing = {"Date", "MSKU", "Disposition", "Location", *NUM} - set(df.columns)
    if missing:
        sys.exit(f"Not an Inventory Ledger Detailed-view export — missing {sorted(missing)}")
    df["_d"] = pd.to_datetime(df["Date"], format="%m/%d/%Y", errors="coerce")
    if df["_d"].isna().mean() > 0.02:
        sys.exit("Dates unparseable (expected MM/DD/YYYY) — different locale export?")
    return df[df["_d"].notna()]


def to_region(loc: pd.Series) -> np.ndarray:
    locs = set(loc.dropna().unique())
    if locs <= COUNTRY_CODES:                      # country-code export
        return np.where(loc == "GB", "GB", "EU")
    unknown_uk = sorted(l for l in locs if l not in UK_FC and l[:3] in UK_PREFIX)
    if unknown_uk:
        print(f"WARNING: UK-looking FCs not in the mapping (counted as EU): {unknown_uk}")
    return np.where(loc.isin(UK_FC), "GB", "EU")


def main(src: str) -> None:
    nw = read_export(src)
    nw = nw[nw["Disposition"] == "SELLABLE"].copy()
    for c in NUM:
        nw[c] = pd.to_numeric(nw[c], errors="coerce").fillna(0)
    nw["region"] = to_region(nw["Location"])
    agg = nw.groupby(["Date", "_d", "MSKU", "region"], observed=True)[NUM].sum().reset_index()

    files = sorted(glob.glob(LEDGER_GLOB))
    if not files:
        sys.exit("No existing ledger in amazon_ledger/ to append to.")
    hist_path = files[-1]
    led = pd.read_csv(hist_path, dtype=str)
    cols = list(led.columns)
    led["_d"] = pd.to_datetime(led["Date"], format="%m/%d/%Y", errors="coerce")
    last = led["_d"].max()
    print(f"Existing ledger {os.path.basename(hist_path)}: "
          f"{led['_d'].min().date()} -> {last.date()}")
    print(f"Export: {agg['_d'].min().date()} -> {agg['_d'].max().date()}, "
          f"{agg['MSKU'].nunique()} SKUs")

    # Overlap check: the new export's EU/GB balance vs the existing ledger.
    ov = sorted(d for d in agg["_d"].unique() if d <= last)
    for d in ov[-3:]:
        a = agg[agg["_d"] == d].groupby("region")["Ending Warehouse Balance"].sum()
        sel = led[(led["_d"] == d) & (led["Disposition"] == "SELLABLE")]
        e = (pd.to_numeric(sel["Ending Warehouse Balance"], errors="coerce")
             .groupby(np.where(sel["Location"].eq("GB"), "GB", "EU")).sum())
        line = " | ".join(f"{r} {int(a.get(r, 0))} vs {int(e.get(r, 0))}" for r in ("EU", "GB"))
        flag = ""
        for r in ("EU", "GB"):
            base = e.get(r, 0)
            if base and abs(a.get(r, 0) - base) / base > 0.02:
                flag = "  <-- differs >2%, check the export"
        print(f"  overlap {pd.Timestamp(d).date()}: {line}{flag}")

    add = agg[agg["_d"] > last]
    if add.empty:
        sys.exit("Nothing new: the export ends on or before the current ledger's last day.")
    out = pd.DataFrame({c: "" for c in cols}, index=add.index)
    out["Date"] = add["Date"]
    out["MSKU"] = add["MSKU"]
    out["Disposition"] = "SELLABLE"
    out["Location"] = add["region"]
    for c in NUM:
        out[c] = add[c].round(0).astype(int).astype(str)
    for c in ZERO_COLS:
        if c in cols:
            out[c] = "0"
    merged = pd.concat([led.drop(columns="_d"), out[cols]], ignore_index=True)
    dest = os.path.join(os.path.dirname(hist_path),
                        f"inventory_ledger_{add['_d'].max().date()}.csv.gz")
    with gzip.open(dest, "wt", compresslevel=6, newline="") as fh:
        merged.to_csv(fh, index=False)
    if os.path.abspath(dest) != os.path.abspath(hist_path):
        os.remove(hist_path)
    dd = pd.to_datetime(merged["Date"], format="%m/%d/%Y", errors="coerce")
    print(f"Appended {add['_d'].min().date()} -> {add['_d'].max().date()} "
          f"(+{len(out):,} rows) -> {os.path.basename(dest)}")
    print(f"Ledger now covers {dd.min().date()} -> {dd.max().date()}. "
          "Commit & push amazon_ledger/ so the dashboard picks it up.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python append_ledger.py <inventory-ledger-export.csv|.txt>")
    main(sys.argv[1])
