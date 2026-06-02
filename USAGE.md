# How to use the dashboard

A 60-second tour of the Margin Analytics app.

## 1. Sign in

Open the app URL, enter the **dashboard password** (Streamlit Cloud → app
settings → Secrets), click **Sign in**.

## 2. Top filter bar

Everything in the page reacts to these. Defaults are sensible — change only
what you need.

| Control | What it does |
| --- | --- |
| **Marketplace** | A single Amazon site, or **🌍 All countries** (default). All-countries rolls each SKU's data across every marketplace. |
| **Granularity** | Day / **Week** / Month / Quarter — drives what the period multiselect shows. |
| **Calendar week(s)** | Pick one or more buckets. Selecting a month or quarter expands to all the days/weeks inside. |
| **Min monthly sales (€, all countries)** | Hides SKUs whose trailing-30-day all-country revenue is below the threshold. Default €2,500. Stops the long tail from cluttering the view. |
| **SKU or Product contains** | Text search. |
| **Top sellers only** | Per-marketplace flag from Novadata. Disabled in All-countries mode. |

## 3. Overview tab

- **KPI tiles** — SKUs in view, total sales, P&L Impact (= Σ CM3 €), avg CM3%, count below target.
- **Per-country breakdown** (All-countries only) — country-level CM3% + a sales-vs-P&L bar chart.
- **3×3 cluster matrix** — color-coded buttons. **Click any cell** to filter the table below to that segment (e.g. "Low margin · High sales"). Click again to clear.
- **Show inventory columns** — toggle to hide FBA/DoS/Velocity columns when you just want margins & sales.
- **Main table** — sortable, horizontally scrollable. Conditional colors: CM3% (orange/green vs target), Δ CM3 / Rev Δ 4w (red/green text), Days of Supply (orange if low), Cluster cells shaded by tier.
- **Comments** — yellow column. Type in any row, hit Enter → saved to a private Gist (if `GITHUB_TOKEN` + `COMMENTS_GIST_ID` are configured) so it survives reboots. Otherwise session-only with a hint shown.

## 4. Margin Trend tab

For the products & period in scope:
- **Trend by:** Month / Quarter / Year selector.
- **Portfolio CM3% line** — blended (Σ CM3 / Σ Sales) per bucket.
- **Rising / Neutral / Declining** — every SKU classified by a sales-weighted linear fit; threshold defaults to ±2 pp.
- Three expandable tables, one per category, each with a CSV download.

## Common workflows

**"Which products are killing my margin?"** → All countries · Latest week · Click the **Low margin · High sales** cell → look at sorted Cluster column.

**"What's been improving in Q2?"** → Granularity = Quarter, pick **Q2 2026** → Margin Trend tab → **Trend by: Month** → expand 📈 Rising margins.

**"Annotate the recurring stock-out SKU"** → find row in the table → type the note in the yellow **Comments** cell → done.

**"Compare DE vs IT margins"** → pick amazon.de, note Avg CM3% → switch to amazon.it → compare. (Or pick All countries and read the per-country breakdown table.)

## Refresh cadence

Data is pulled daily at 06:00 UTC (margin) and 06:30 UTC (products/FBA) via
GitHub Actions. The dashboard reads whichever `margin_export_YYYY-MM-DD.csv.gz`
is newest in the repo. Manual refresh: GitHub → **Actions → Run workflow**.

## Threshold tuning

Code defaults in `streamlit_app.py`:
- `target_cm3 = 19.7` (CM3% target — drives orange/green cell colors)
- `min_dos = 30` (Days of Supply — drives the orange low-stock highlight)

Edit those values and push to change site-wide.
