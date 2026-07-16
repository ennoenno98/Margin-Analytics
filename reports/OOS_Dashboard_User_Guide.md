# OOS Impact Analytics — User Guide

_A quick guide for the marketing team. No technical knowledge needed._

## What this dashboard is for

It answers one question: **how much revenue and margin are we losing because
products aren't available on Amazon — and which products are worst?**

Every day it compares what each product *actually* sold against what it
*would* have sold if it had been in stock, and turns the gap into euros. Use it
to spot which SKUs to prioritise for restocking, which listings need attention,
and how much a stock-out really cost.

> **One rule to remember:** the headline number is **Lost CM3** — the
> contribution margin (real profit) we gave up. **Lost revenue** is the
> top-line version of the same thing.

---

## Getting in

Open the dashboard link and enter the shared password. That's it — the data
refreshes automatically, so you always see the latest.

---

## The filter bar (top of the page)

Five controls sit across the top. Set these first; everything below updates
instantly.

| Filter | What it does |
| --- | --- |
| **Region** | **EU** (all European marketplaces, pooled), **GB** (the UK / amazon.co.uk warehouse), or **EU + UK (combined)** for the total picture. EU and UK stock are separate, so they're tracked apart by default. |
| **Period** | Full range, or drill into a Year / Quarter / Month / Week. You can pick more than one (e.g. two months). |
| **Min demand** | How busy a product must be before a no-sales day counts as a stock-out. Leave at the default (3/day) unless you're investigating slow movers. |
| **SKU or Product contains** | Type part of a product name or SKU to focus on one product or a range. |

There's also a **"Stock-out, cooling-down & heating-up thresholds"** expander.
You can ignore it — the defaults are set with Ops. Open it only if you want to
tune sensitivity (e.g. how tight stock must be to count as "critically low").

---

## The headline (top of the page, under the filters)

- A **chart** of lost revenue, lost CM3, and the number of SKUs out of stock,
  over time (by month or quarter).
- **KPI tiles** grouped by type:
  - **Lost** — sales we *couldn't* make because we were out of stock (the real
    problem).
  - **Miss** (Cooling down) — sales we *chose* to give up by deliberately
    easing off (raising price / cutting ads) to stretch tight stock. Voluntary,
    so it's kept separate.
  - **Ramp-up** (Heating up) — the cost of getting a product going again after
    it came back.
  - **🚫 Listing blocked** — products sitting on healthy stock but **not
    selling at all** — a sign the listing may be suppressed or not buyable.
    Shown as "unrealized" revenue and, importantly, **not** counted as a
    stock-out. **Check these listings.**
- **OOS rate** — share of time products were out of stock.
- **WISR** — in-stock rate weighted by how important each product is (a
  stock-out on a big seller hurts this more than one on a small one). Higher is
  better.

---

## The tabs — where to look for what

| Tab | Use it to… |
| --- | --- |
| **Most affected SKUs** | See the ranked list of biggest losses. Click a SKU to get a timeline of its stock and sales. **Start here.** |
| **Stock-out calendar** | A month-by-month heatmap — spot seasonal or recurring stock-out patterns per product. |
| **Stock-out events** | Every individual stock-out as a row (start, end, how long, cause, € lost). Download as CSV for sharing. |
| **Cooling down** | Products we deliberately throttled to protect tight stock — and what that cost. |
| **Heating up** | Products in recovery after coming back — extra ad spend + sales still ramping. |
| **Country overview** | Which countries carry the loss, valued at each country's own price and margin. Pick a country to drill in. |
| **Top sellers** | A watchlist of the highest-value products and their current stock status — a stock-out here matters most. |

Most tables have a **download button** for exporting to Excel/CSV.

---

## Reading a "cause" label

When a product isn't selling normally, the dashboard says why:

- **Physical (network)** — genuinely zero stock in the warehouse.
- **Critically low (reach)** — almost out; days of stock left below the safe
  threshold.
- **Demand gap** — in stock on paper but selling nothing, and it should be
  selling — usually a real availability problem.
- **Suppressed sales (post-OOS)** — still recovering after a stock-out.
- **Cooling down** — we eased off on purpose (not a problem).
- **Listing blocked (in stock)** — plenty of stock, zero sales → **a listing
  issue to investigate**, not a stock-out.
- **Heating up** — being pushed back to full speed after returning.

---

## A typical workflow

1. Pick your **Region** and **Period** (e.g. EU, last quarter).
2. Look at the **headline KPIs** — how much did we lose, and is the trend up or
   down?
3. Open **Most affected SKUs** and note the top few by Lost CM3.
4. Click one to see *when* and *why* it went out — was it a real stock-out, a
   deliberate throttle, or a blocked listing?
5. Check the **🚫 Listing blocked** list — these are quick wins (a listing fix,
   no restock needed).
6. Export **Stock-out events** or the ranked table to share with Ops /
   Supply Chain.

---

## Good to know

- **Numbers are estimates, not the accounting P&L.** They measure lost
  *opportunity* using Amazon's own stock and sales data, so they won't tie out
  to the cent with finance (timing differences). They're for prioritising, not
  for booking.
- **EU vs UK are separate warehouses** — that's why they're split. Use
  **combined** for the total, but restocking decisions are per-region.
- **The newest day is always one day behind** — the current day's data is
  partial, so it's excluded until it's complete.
- Everything is shown in **euros, whole numbers, European formatting**
  (1.234,5), with **English product names**.

---

_Questions or a metric that looks off? Flag it — the model's thresholds are all
tunable and documented in the methodology sheet._
