# OOS Impact Analytics

A sibling Streamlit dashboard to [Margin Analytics](README.md) that tracks
**Amazon out-of-stock (OOS) impact over time** — estimated lost revenue and
contribution margin per SKU — and ranks **which SKUs are most affected over the
year**. (Voids doesn't surface this.)

It is a **separate app** (`oos_analytics.py`, its own URL / Streamlit deploy)
but lives in this repo so it can reuse the daily Novadata export, the password
gate and the deploy plumbing.

## Hybrid data model

It combines two Amazon sources:

| Source | Path | Gives |
| --- | --- | --- |
| **FBA Inventory Ledger** | `amazon_ledger/inventory_ledger_*.csv.gz` | Real daily warehouse balance + customer shipments per SKU. The seller runs **Pan-EU**, so stock is pooled across the network → physical OOS only when the whole EU balance hits zero. Powers true physical stock-outs, actual demand, days-of-supply / low-stock risk. |
| **Novadata margin export** | `novadata_exports/margin_export_*.csv.gz` | Per-SKU per-marketplace daily Units / Sales / CM3 → marketplace-level lost sales and the price + margin per unit to value lost units in €. |

**Why hybrid?** With Pan-EU pooling the network is almost never at literally
zero stock (≈ a few dozen SKU-days a year), so a "balance == 0" rule alone
barely fires. The bulk of real lost sales happens at the **marketplace** level
(a SKU that normally sells in DE suddenly goes quiet) — which the Novadata
signal catches. The ledger then confirms the physical truth and powers the
forward-looking risk view.

### How OOS is defined

A SKU × marketplace is **out of stock** on a day when **any** of:

1. **Physical (network):** EU sellable balance == 0 in the ledger.
2. **FBA snapshot:** Novadata `FBA Available` == 0.
3. **Marketplace gap:** `Units == 0` on a day *enclosed by sales* (not a
   pre-launch / discontinued tail), for a SKU whose demand rate clears
   **Min demand (units/day)** — high enough that selling nothing is a genuine
   anomaly (this filter is what stops a thin marketplace's normal no-sale days
   from being mistaken for stock-outs).

Each OOS day is tagged with its **cause**: *Physical (network)* when the EU pool
is empty, otherwise *Marketplace gap* — sales stopped in that marketplace
despite EU stock (offer suppression, buy-box loss, listing issue, …). Under
Pan-EU an empty *local* warehouse is not a stock-out (the pool fulfils it), so
there's no separate local cause.

### How impact is valued

- **Lost units** = expected daily demand − whatever still sold that day.
  Expected demand = trailing 90-day average units **per calendar day** (the
  demand rate), forward-filled so a multi-week stock-out keeps its pre-outage
  baseline.
- **Lost revenue (€)** = lost units × trailing avg selling price.
- **Lost CM3 (€)** = lost units × trailing avg contribution margin per unit —
  the true **P&L impact**, the headline number the ranking sorts by.

Thresholds live at the top of `oos_analytics.py`
(`DEFAULT_MIN_DEMAND`, `BASELINE_WINDOW`, `LOW_STOCK_DAYS`, …).

## The dashboard

- **Most affected SKUs** — KPI tiles + a ranked table (lost revenue / lost CM3,
  OOS days, OOS rate, current stock & days-of-supply) and a top-N bar chart.
- **Impact over time** — lost € + OOS-days by month/quarter, a SKU × month
  stock-out heatmap, and a single-SKU drill-down (units vs expected demand vs
  EU stock, OOS days shaded).
- **Inventory & risk** — ledger-driven: SKUs currently out of stock, low-stock
  (< days-of-supply threshold), units in transit, sorted replenish-first.
- **Stock-out events** — every discrete stock-out collapsed into an event
  (start / end / duration / cause / lost €), with CSV export.

## Refreshing the ledger (manual)

The margin export refreshes automatically (GitHub Actions, daily). The Amazon
Inventory Ledger is uploaded **manually** when you want fresh stock data:

1. **Seller Central → Reports → Fulfilment → Inventory Ledger → "Detailed
   View"**, set the date range to the trailing ~12 months, download the CSV.
2. Run the helper, then commit:
   ```bash
   python add_ledger.py ~/Downloads/inventory-ledger.csv
   git add amazon_ledger && git commit -m "data: ledger refresh" && git push
   ```
   It gzips the file, date-stamps it by the latest date inside, and prunes old
   snapshots. The dashboard always reads the newest one. The app still runs
   (Novadata signals only) if no ledger is present.

## Deploy

Same as Margin Analytics — deploy a **second** Streamlit Community Cloud app
from this repo with **Main file path = `oos_analytics.py`** and the same
`DASHBOARD_PASSWORD` secret. `requirements.txt` already covers it (no new deps).

## Local dev

```bash
pip install -r requirements.txt
python novadata_weekly_export.py --once            # margin export
python add_ledger.py /path/to/inventory-ledger.csv # optional: ledger
export DASHBOARD_PASSWORD="choose-a-password"
streamlit run oos_analytics.py                     # → http://localhost:8501
```
