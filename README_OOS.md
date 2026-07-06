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
| **FBA Inventory Ledger** | `amazon_ledger/inventory_ledger_*.csv.gz` | Real daily warehouse balance + customer shipments per SKU. The seller runs **Pan-EU**, so stock is pooled across the network → physical OOS only when the whole EU balance hits zero. **GB is a separate (post-Brexit) warehouse, excluded from the EU pool — and amazon.co.uk demand is excluded from EU λ** so the two stay consistent. Powers true physical stock-outs, actual demand, days-of-supply / low-stock risk. |
| **Novadata margin export** | `novadata_exports/margin_export_*.csv.gz` | Daily Units / Sales / CM3 per SKU, **pooled EU-wide** (Pan-EU — not split by country) → the demand rate and the price + margin per unit to value lost units in €. |

**Why hybrid + EU-pooled?** The account runs **Pan-EU**, so demand and stock are
pooled across marketplaces — splitting λ by country understates true demand, so
everything is computed at **SKU / EU level** (one row per SKU per day). With
pooling the network is rarely at literally zero stock, so a "balance == 0" rule
alone barely fires; the reach and demand-gap signals carry most of the load, and
the ledger powers the forward-looking risk view.

### How OOS is defined

A SKU is **out of stock** on a day when **any** of:

1. **Physical (network):** EU sellable balance == 0 in the ledger.
2. **Critically low (<3d):** reach (days-of-supply) below 3 — effectively out
   even if the balance isn't literally zero.
3. **Demand gap (EU):** EU `Units == 0` on a day *enclosed by sales* (not a
   pre-launch / discontinued tail), for a SKU whose EU demand rate clears
   **Min demand (units/day)** — high enough that selling nothing is a genuine
   anomaly (this stops slow movers' normal no-sale days being mistaken for
   stock-outs).

Cause priority: **Physical (network) > Critically low (<3d) > Cooling down >
Demand gap (EU)**.

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

A **Region** toggle at the top switches between **🇪🇺 EU** (the Pan-EU pool) and
**🇬🇧 GB** (the separate UK warehouse / amazon.co.uk) — each tracked as an
independent pool (own stock, demand λ, reach and impact).

- **Most affected SKUs** — a headline **OOS-impact-over-time** chart (lost
  revenue + lost CM3 + # SKUs out of stock, by month/quarter), KPI tiles, a
  ranked table, a top-N bar chart and a per-SKU timeline drill-down.
- **Stock-out calendar** — a SKU × month heatmap of stock-out days.
- **Country overview** — OOS impact per country from the **actual items lost in
  each country** (each SKU's EU lost units split by its unit share per
  marketplace) valued at **that country's own price & CM3**, so the same
  stock-out weighs differently by country. Pick a country to drill into its SKUs.
- **Stock-out events** — every discrete stock-out collapsed into an event
  (start / end / duration / cause / lost €), with CSV export.
- **Cooling down** — days the SKU was *deliberately throttled* (ad spend cut
  and/or price raised) while stock was tight, to glide to the next shipment
  instead of hard stocking out. Reports the **revenue miss** and **CM3 miss**
  (the sales voluntarily forgone), per SKU, with CSV export.
- **Top sellers** — OOS tracker for the highest-value SKUs, ranked by
  **expected revenue** (λ × avg price — a stable base a stock-out can't shrink),
  with current status/reach. The header also shows **WISR** (Weighted In-Stock
  Rate): % of time in stock, weighted by expected revenue.
- **Heating up** — the **ramp-up after a SKU returns** (from cooling-down or a
  stock-out): ad spend pushed up and/or price cut (only if previously raised)
  to rebuild momentum. Reports **ramp-up lost sales** (still below baseline λ
  while recovering) + the **extra ad spend** vs baseline. Defaults: ad +50%,
  price −10%, 28-day window — *provisional, pending Logistics/Ops input*; all
  tunable in the app.

### Cooling down vs. stock-out (don't double-count)

A deliberate PPC cut or price hike looks like a demand drop to the gap signal,
so the model separates them. Reach (days-of-supply) drives the split:

- **reach below the region threshold → OOS** ("Critically low") — effectively
  out of stock even if the balance isn't literally zero. Defaults per Ops:
  **EU 4 days** (≈ dispatch-to-sellable), **GB 12 days** (transfers + customs).
- **3 ≤ reach < 30 days + throttled + still selling → Cooling down** — the
  SKU's **ad spend is cut by ≥ 70 % vs its baseline** and/or its **price is
  ≥ 15 % above baseline**, while it still sells (units > 0). An **ad cut only
  counts if the price is not simultaneously discounted** (pulling ads back while
  discounting is a promo ending, not a throttle). If a throttle pushes sales to
  **zero** it's not cooling down — it counts as OOS (lost), not miss.

Cooling-down impact is booked as *miss* (voluntary), kept apart from involuntary
*lost*. Category priority per day: **Physical (network) > Critically low (<3d) >
Cooling down > Demand gap (EU)**. All thresholds are tunable in the app.

**Returns don't end a stock-out.** Customer returns trickle back into the
warehouse and can nudge the sellable balance/reach up mid-stock-out, which would
otherwise break the OOS run (or trip a spurious cooling-down flag). So once a SKU
is OOS it stays OOS — through return-driven blips — until either a genuine
inbound **Receipt** arrives or sales recover to ≥ ½·λ. Receipts (real inbound)
and Customer Returns are separate ledger columns, so the two are told apart.

Caveat: ad-spend-cut detection only
works from when Novadata began reporting Advertising Costs (~Feb 2026); the
price-hike lever spans the full year.

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
