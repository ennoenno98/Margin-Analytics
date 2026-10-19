"""Import the ABCD product clusters from a JTL "Eigene Felder Artikel" export.

JTL exports custom article fields in long format — one row per article and
field, semicolon-separated, Windows-1252 encoded:

    "Artikelnummer";"Artikelname";"<Feldname>";"<Wert>";

This script keeps the "ABCD-Cluster" field and writes `product_clusters.csv`
(SKU, Product, Cluster), which the OOS dashboard reads for its Cluster filter
and Clusters tab. Cluster values: A, B, C, D (ABCD analysis), NPL (new product
launch), EOL (end of life).

Usage:
    python add_clusters.py /path/to/JTL-Export-Eigene_Felder_Artikel-*.csv
    # then: git add product_clusters.csv && git commit && git push
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "product_clusters.csv"
FIELD = "ABCD-Cluster"
VALID = {"A", "B", "C", "D", "NPL", "EOL"}


def main(src: str) -> None:
    raw = pd.read_csv(src, sep=";", encoding="cp1252", dtype=str, header=0,
                      usecols=[0, 1, 2, 3], names=["SKU", "Product", "Field", "Value"])
    c = raw[raw["Field"].str.strip() == FIELD].copy()
    if c.empty:
        sys.exit(f"No '{FIELD}' rows found — is this a JTL 'Eigene Felder Artikel' export?")
    c["SKU"] = c["SKU"].str.strip()
    c["Cluster"] = c["Value"].fillna("").str.strip().str.upper()
    bad = c[~c["Cluster"].isin(VALID)]
    if not bad.empty:
        print(f"Skipping {len(bad)} rows with unknown cluster values: "
              f"{sorted(bad['Cluster'].unique())}")
    c = c[c["Cluster"].isin(VALID)].drop_duplicates("SKU", keep="last")
    c[["SKU", "Product", "Cluster"]].sort_values("SKU").to_csv(OUT, index=False)
    print(f"Wrote {OUT.name}: {len(c)} SKUs — "
          + ", ".join(f"{k} {v}" for k, v in c["Cluster"].value_counts().items()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python add_clusters.py <JTL-Export-Eigene_Felder_Artikel.csv>")
    main(sys.argv[1])
