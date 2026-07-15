# OOS Lost Revenue — Methodology

_How the OOS Impact Analytics dashboard estimates the revenue and contribution
margin (CM3) lost to Amazon out-of-stocks. Source of truth: `oos_analytics.py`._

---

## 1. The question

For every SKU, on every day, we observe **units sold** and (from the Amazon FBA
Inventory Ledger) **stock on hand**. A day with **fewer sales than usual** is
either a genuine availability problem (lost sales we want to quantify) or just
normal demand variation. Lost revenue is:

> **Lost = what the SKU *would* have sold − what it *actually* sold**, on days
> we judge it was out of stock, valued in € and in CM3.

Everything is computed at **SKU × region × day**. Two regions run as independent
pools: **EU** (the Pan-EU network, all marketplaces except amazon.co.uk) and
**GB** (the separate post-Brexit UK warehouse). Demand is pooled EU-wide — never
split by country — because Pan-EU stock is shared across marketplaces.

---

## 2. Expected demand (λ) — the counterfactual baseline

The "would have sold" figure is the SKU's **demand rate λ**:

```
λ = trailing-90-day average units per LIVE day
```

- **Live days only.** The 90-day window averages only days the SKU was
  genuinely sellable. Two kinds of day are **excluded** from the baseline:
  - **pre-launch days** (before the SKU's first-ever sale) — otherwise a new
    SKU's λ is diluted by dozens of structural zeros;
  - **long zero-runs** (≥ 7 consecutive zero-sales days — an outage or a blocked
    listing, not sales noise) — otherwise λ would *decay through the stock-out*
    and a long outage would eventually value itself at ≈ 0 lost sales.
- **Frozen through the outage.** With those days masked, λ is carried forward
  (`ffill`) at the last live rate, so a multi-week stock-out keeps its
  pre-outage demand rate.
- **Short scattered zeros still count**, so a genuinely slow mover keeps its
  true low rate and its ordinary no-sale days are *not* mistaken for stock-outs.

The newest day in the export is an **intra-day partial** snapshot and is always
dropped before any of this runs (it would otherwise look like a morning-long
stock-out every day).

### Why a rate, and the Poisson intuition
Daily sales behave roughly like a Poisson process. Under Poisson the chance of a
zero-sales day is `P(0) = e^(−λ)`:

| λ (units/day) | P(0 sales) | reading |
| --- | --- | --- |
| 1 | ~37 % | zeros are normal → ignore |
| 2 | ~14 % | still common |
| **3 (default)** | **~5 %** | zero is anomalous → flag |
| 5 | ~0.7 % | near-certain block |
| 30 | ~0 % | unmistakable |

So a zero-sales day only counts as a demand-gap stock-out when **λ ≥ 3
units/day** (`DEFAULT_MIN_DEMAND`, tunable) — the ~5 % / 2-sigma cutoff. Below
that, zeros carry no signal.

---

## 3. When is a day "out of stock"?

A SKU-day is flagged **OOS** if **any** of these hold (and it isn't a deliberate
throttle or a listing problem — see §4):

1. **Physical (network)** — EU sellable balance ≤ 0 in the ledger.
2. **Critically low** — reach (days-of-supply) below the region threshold:
   **EU 4 days**, **GB 12 days** (`OOS_DOS_EU` / `OOS_DOS_GB`; EU ≈
   dispatch-to-sellable, GB longer for transfers + customs). Effectively out
   even before the balance literally hits zero.
3. **Demand gap** — units = 0 on a day *enclosed by sales* (not a pre-launch or
   discontinued tail), for a SKU whose λ ≥ min-demand.

**Reach (days-of-supply)** = available stock ÷ trailing-28-day average daily
shipments, where **available = on-hand + in-transit between fulfilment
centres**.

### Episodes don't break on a returns blip
Customer returns trickle back into the warehouse and can nudge the balance/reach
up mid-stock-out. An OOS **episode stays open** until stock *demonstrably*
recovers:
- a meaningful inbound **Receipt** (scaled to demand — several partial
  deliveries don't each have to clear a fixed floor), **or**
- **reach climbs back above the cool-down band**, **or**
- sales recover to **≥ ½ of the effective rate**.

Bridged days that still carry some sales are labelled *Suppressed sales
(post-OOS)*, distinct from a true zero-sales *Demand gap*.

---

## 4. What is deliberately NOT counted as lost

| Category | Rule | Booked as |
| --- | --- | --- |
| **Cooling down** | Reach tight (below `COOLDOWN_DOS` = 30 d, above the OOS threshold) **and** a deliberate throttle — ad spend cut ≥ 70 % vs baseline and/or price raised ≥ 15 % — while the SKU still sells. An ad cut only counts if the price isn't simultaneously discounted (that's a promo ending, not a throttle). | **Miss** (voluntary), kept separate from involuntary *lost* |
| **Listing blocked (in stock)** | units = 0 with **reach > 15 days** (`BLOCKED_MIN_REACH`). Plenty of stock but no sales → a listing/offer problem (suppressed / not buyable), not a stock-out. Workaround for the missing Seller Central suppression report. | **Unrealized** revenue/CM3, excluded from OOS totals |
| **Heating up** | The ramp-up *after* a SKU returns (ad push and/or price cut within 28 d of a disruption, still below 90 % of baseline). | **Ramp-up lost** + **extra ad spend** |

**Category priority per day:** Physical → Critically low → Cooling down →
Demand gap → Listing blocked → Heating up. The flags are mutually exclusive, so
no day is double-counted.

---

## 5. Valuing the lost units

On each OOS day:

```
lost_units   = max( expected_effective − units_sold , 0 )
lost_revenue = lost_units × trailing-avg selling price per unit
lost_CM3     = lost_units × trailing-avg contribution margin per unit
```

- **`expected_effective`** is normally λ, **but** if the SKU was recently
  *positioned* to sell faster than usual, the positioned run-rate replaces λ:
  - **Positioned** = price cut ≥ 5 % (with ads at/above baseline) **or** an ad
    boost ≥ 1.5× baseline — e.g. a Prime Day push.
  - The average sales rate over such days (remembered 21 days) is used **only
    when it clearly exceeds λ (≥ 1.25×)** — so a stock-out during a promo window
    is valued against the elevated rate the SKU was actually running, not its
    long-run average.
- **Price and CM3 per unit** are each the SKU's own trailing-90-day averages, so
  the valuation reflects that SKU's real economics, not a blended number.

**Lost CM3 is the headline P&L number** — it's what the ranking sorts by and
what the "true cost" totals report. Lost *revenue* is the top-line equivalent.

### Country split (Country overview tab)
EU lost units are allocated to countries by each SKU's **actual unit share per
marketplace** (measured over the full period, so a fully-OOS SKU in the selected
window isn't dropped), then valued at **that country's own price and CM3**. So
the same stock-out weighs differently in a high-margin market (DE) than a thin
one (ES).

---

## 6. Rolled-up metrics

- **OOS rate** — share of live SKU-days flagged OOS.
- **WISR** (Weighted In-Stock Rate) — % of time in stock, weighted by each
  SKU's **expected revenue** (λ × avg price), so a stock-out on a big seller
  hurts the score more than one on a slow mover. A stable base a stock-out
  can't shrink.

---

## 7. Data sources & honest caveats

- **FBA Inventory Ledger** (Detailed view) → daily balances, shipments,
  receipts, reach. Manually refreshed or via SP-API; the tail can be rolled
  forward from a Transaction-view export (synthesized rows, clipped at 0).
- **Novadata margin export** → daily units, sales, CM3, ad spend per SKU.
- **Amazon vs accounting:** OOS uses the Amazon ledger (what the customer sees);
  it won't reconcile to the cent with the accounting P&L (pending units, order
  vs fulfilment dating — typically a 7–14 % timing gap).
- **Ad-cut detection** only works from when Novadata began reporting Advertising
  Costs (~Feb 2026); the price-hike lever spans the full year.
- Real sales are somewhat more variable than pure Poisson (weekends,
  promotions); the P(0) percentages are decision intuition, and every threshold
  in §2–§4 is exposed as a slider so it can be tuned.

---

_All thresholds live at the top of `oos_analytics.py`. This document describes
the model as deployed; the interactive dashboard is the source of truth._
