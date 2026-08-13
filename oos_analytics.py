"""Out-of-Stock (OOS) Impact Analytics dashboard (Streamlit).

Sibling tool to `streamlit_app.py` (Margin Analytics). Deployed as its own
Streamlit app (separate main-file path) but lives in this repo so it can reuse
the daily Novadata Amazon export and the password gate.

Question it answers:
    "How much revenue / contribution margin did we lose to stock-outs over the
     year, and which SKUs are most affected?"

HYBRID data model — it combines two Amazon sources:

  1. Amazon FBA Inventory Ledger  (amazon_ledger/inventory_ledger_*.csv.gz)
     The authoritative daily warehouse balance + customer shipments per SKU.
     The seller runs Pan-EU, so availability is pooled across the network and a
     SKU is physically out of stock only when the *whole* EU sellable balance
     hits zero. Gives the true physical stock-out signal, real demand (units
     shipped), and forward-looking days-of-supply / low-stock risk. Refreshed by
     uploading a new export to amazon_ledger/ (see README_OOS.md). NOTE: under
     Pan-EU the network is almost never at literally zero stock, so a balance==0
     rule alone barely fires — which is why we layer the marketplace signal
     below.

  2. Novadata daily margin export  (novadata_exports/margin_export_*.csv.gz)
     Daily Units / Sales / CM3 per SKU. The account runs Pan-EU, so these are
     pooled EU-wide per SKU (not split by country — a per-country split
     understates true demand). Gives the demand rate and the price + CM3 per
     unit needed to value lost units in €.

Two independent regions are tracked, switchable in the dashboard: **EU** (the
Pan-EU pool — all marketplaces except amazon.co.uk) and **GB** (the separate
post-Brexit UK warehouse — amazon.co.uk). Within each region everything is
computed at SKU level (one row per SKU per day). A SKU is OUT OF STOCK on a day
when ANY of:
  * the EU sellable balance is 0 (real physical stock-out — ledger), OR
  * reach (days-of-supply) < 3 (critically low — effectively out), OR
  * EU units == 0 on a day enclosed by sales, with the EU demand rate high
    enough that selling nothing is a real anomaly (a demand gap).
Cause priority: Physical (network) > Critically low (<3d) > Cooling down >
Demand gap (EU).

Lost units = expected daily demand − whatever still sold; valued at the SKU's
trailing avg price (→ lost revenue) and avg CM3 per unit (→ lost CM3, the P&L
impact).

Separately, "Cooling down" days are when demand was *deliberately throttled*
(ad spend cut and/or price raised) while stock was tight and the SKU was still
selling (units > 0), to avoid a hard stock-out. Their forgone sales are booked
as revenue/CM3 *miss* (voluntary) rather than *lost* (involuntary), so the two
are never double-counted.

"Heating up" days are the *ramp-up after a SKU returns* (from a cooling-down or
a stock-out): ad spend is pushed back up and/or price cut (only if we'd raised
it) to rebuild sales. It costs us twice — ramp-up lost sales (still below
baseline λ while recovering) + the extra ad spend over baseline — tracked
separately. A stock-out isn't required; heat-up can follow a cooling-down
directly. Thresholds are provisional, pending Logistics/Ops input.

Run locally:
    pip install -r requirements.txt
    python novadata_weekly_export.py --once       # margin export
    # drop an FBA Inventory Ledger CSV into amazon_ledger/ (or use upload tool)
    export DASHBOARD_PASSWORD="choose-a-password"
    streamlit run oos_analytics.py
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import brand

REPO_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = REPO_ROOT / "novadata_exports"
LEDGER_DIR = REPO_ROOT / "amazon_ledger"

# Demand baseline window + OOS heuristic defaults.
BASELINE_WINDOW = 90       # trailing window for expected demand / price / CM3
DEFAULT_MIN_DEMAND = 3.0   # min expected units/day to infer a marketplace gap
TAIL_DAYS = 21             # treat trailing zero-runs near "today" as ongoing OOS
LOW_STOCK_DAYS = 21        # days-of-supply threshold for the low-stock risk view
OOS_DOS_EU = 4             # EU reach threshold — ~4d dispatch-to-sellable (Ops input)
OOS_DOS_GB = 12            # GB reach threshold — longer transfers + customs (Ops input)
OOS_DOS = OOS_DOS_EU       # fallback default for flag_oos()

# "Cooling down" = deliberately throttling demand (cutting PPC and/or raising
# price) while stock is tight, to glide to the next shipment instead of hard
# stocking out (OOS hurts Amazon ranking). Defaults for detecting it:
COOLDOWN_DOS = 30          # only when days-of-supply is at/below this (tight)
COOLDOWN_PPC_CUT = 0.7     # ad spend <= this fraction below baseline = an ad cut
COOLDOWN_PRICE_UP = 0.15   # price >= baseline x (1+this) = a deliberate hike
COOLDOWN_MIN_PPC = 2.0     # ignore SKUs whose baseline ad spend < this (EUR/day)

# "Heating up" = ramp-up after a SKU returns (from cooling-down or stock-out):
# we lower price (only if we'd raised it) and/or push ad spend to rebuild sales.
# It costs us twice — ramp-up lost sales (still below baseline) + extra ad spend.
HEATUP_AD_UP = 0.50        # ad spend >= baseline x (1+this) = a ramp-up push
HEATUP_PRICE_DOWN = 0.10   # price <= baseline x (1-this) = a re-stimulation cut
HEATUP_WINDOW = 28         # recovery window (days) after the disruption
HEATUP_RECOVERED = 0.9     # sales back to this x baseline = ramp-up over

# Promo-elevated counterfactual: when a SKU was recently *positioned* to sell
# faster than usual (price cut and/or ad push, e.g. Prime Day), a throttle or
# stock-out in that window forgoes the POSITIONED run-rate, not the slow 90d
# lambda — so losses in that window are valued at the positioned rate.
PROMO_PRICE_CUT = 0.05     # price <= baseline x (1-this) counts as positioned
PROMO_AD_UP = 1.5          # ad spend >= baseline x this counts as positioned
PROMO_WINDOW = 21          # look-back (days) for the positioned run-rate
PROMO_ELEV = 1.25          # positioned rate must exceed this x lambda to apply

# Listing blocked/suppressed workaround (no Seller Central report exists):
# zero sales with plenty of stock is a listing problem, not a stock-out.
BLOCKED_MIN_REACH = 15     # units==0 & reach ABOVE this => "Listing blocked", not OOS

# Marketplace -> country label (for the country breakdown).
MKT_COUNTRY = {
    "amazon.de": "🇩🇪 Germany", "amazon.fr": "🇫🇷 France", "amazon.it": "🇮🇹 Italy",
    "amazon.es": "🇪🇸 Spain", "amazon.nl": "🇳🇱 Netherlands",
    "amazon.com.be": "🇧🇪 Belgium", "amazon.ie": "🇮🇪 Ireland",
    "amazon.se": "🇸🇪 Sweden", "amazon.co.uk": "🇬🇧 United Kingdom",
}

st.set_page_config(page_title="OOS Impact Analytics", page_icon="📦", layout="wide")
brand.apply()   # Vanatari corporate design — brand CSS + Plotly template


# ---------- Auth (mirrors streamlit_app.py) ----------
def _get_password() -> str | None:
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if pw:
        return pw
    try:
        return st.secrets["DASHBOARD_PASSWORD"]
    except Exception:
        return None


def require_login() -> None:
    expected = _get_password()
    if not expected:
        st.error(
            "DASHBOARD_PASSWORD is not set. Configure it in the host's "
            "environment variables (Render: Service → Environment) or in "
            "`.streamlit/secrets.toml` for local dev."
        )
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("📦 OOS Impact Analytics")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ---------- File discovery ----------
def _latest(directory: Path, *patterns: str) -> Path | None:
    if not directory.exists():
        return None
    cands: list[Path] = []
    for pat in patterns:
        cands += list(directory.glob(pat))
    return sorted(cands)[-1] if cands else None


def latest_export(d: Path) -> Path | None:
    return _latest(d, "margin_export_*.csv", "margin_export_*.csv.gz")


def latest_ledger(d: Path) -> Path | None:
    return _latest(d, "inventory_ledger_*.csv", "inventory_ledger_*.csv.gz")


def latest_discontinued(d: Path) -> Path | None:
    """Newest product-discontinuation export (a weekly DB pull). The filename is
    flexible — anything containing 'discontinued' as .xlsx / .csv / .csv.gz."""
    return _latest(d, "*discontinued*.xlsx", "*discontinued*.csv",
                   "*discontinued*.csv.gz")


# ---------- Loaders ----------
@st.cache_data(show_spinner=False)
def load_margin(path: Path, mtime: float | None = None) -> pd.DataFrame:
    """Daily Novadata margin export, trimmed to what the OOS model needs.

    `mtime` is part of the st.cache_data key so an in-place overwrite of the
    same filename invalidates the cache."""
    KEEP = {
        "Period", "SKU", "Product", "Marketplace Name", "Brand",
        "Units", "Product Sales", "Contribution Margin 3", "Advertising Costs",
        "FBA Available", "Sales Velocity",
    }
    df = pd.read_csv(path, usecols=lambda c: c in KEEP)
    df["Period"] = pd.to_datetime(
        df["Period"], errors="coerce", utc=True
    ).dt.tz_localize(None)
    if "Contribution Margin 3" in df.columns:
        df["CM3"] = pd.to_numeric(df["Contribution Margin 3"], errors="coerce",
                                  downcast="float")
        df = df.drop(columns=["Contribution Margin 3"])
    # Advertising Costs is stored negative (a cost) and only reported from the
    # date Novadata began emitting it (currently ~2026-02). Flip to positive
    # ad spend; absent history simply yields zeros (no PPC-cut detection there).
    if "Advertising Costs" in df.columns:
        df["AdSpend"] = (-pd.to_numeric(df["Advertising Costs"], errors="coerce")
                         ).clip(lower=0).astype("float32")
        df = df.drop(columns=["Advertising Costs"])
    else:
        df["AdSpend"] = np.float32(0.0)
    for col in ("Units", "Product Sales", "FBA Available", "Sales Velocity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce", downcast="float")
    df = df.rename(columns={"Product Sales": "Sales"})
    for col in ("SKU", "Marketplace Name", "Product", "Brand"):
        if col in df.columns:
            df[col] = df[col].astype("category")
    return df


@st.cache_data(show_spinner=False)
def load_ledger(path: Path | None, mtime: float | None = None):
    """Amazon FBA Inventory Ledger → daily stock panel per SKU and region.

    Returns one row per (SKU, region, Date). `eu_stock` is **available** stock =
    on-hand sellable + units in transit between FCs (so a Pan-EU redistribution
    isn't read as a stock drop); `in_transit` and `on_hand` are also kept.
    Region is **EU** (the Pan-EU pool) or **GB** (the separate post-Brexit
    warehouse), kept apart so each is its own pool. Empty if no ledger present.
    """
    empty_eu = pd.DataFrame(
        columns=["SKU", "region", "Date", "eu_stock", "shipped", "in_transit",
                 "receipts"])
    if path is None:
        return empty_eu
    KEEP = {
        "Date", "MSKU", "Location", "Disposition",
        "Ending Warehouse Balance", "Customer Shipments",
        "In Transit Between Warehouses", "Receipts",
    }
    led = pd.read_csv(path, usecols=lambda c: c in KEEP)
    led["Date"] = pd.to_datetime(led["Date"], format="%m/%d/%Y", errors="coerce")
    if led["Date"].isna().mean() > 0.02:
        raise ValueError(
            "Inventory-ledger dates unparseable (expected MM/DD/YYYY, e.g. "
            "'07/06/2026') — was the export downloaded in a different locale?")
    led = led[led["Date"].notna()]
    led = led.rename(columns={"MSKU": "SKU"})
    for c in ("Ending Warehouse Balance", "Customer Shipments",
              "In Transit Between Warehouses", "Receipts"):
        led[c] = pd.to_numeric(led.get(c), errors="coerce").fillna(0.0)
    led = led[led["Disposition"] == "SELLABLE"].copy()
    # GB is a separate (post-Brexit) warehouse, NOT part of the Pan-EU pool.
    led["region"] = np.where(led.get("Location") == "GB", "GB", "EU")

    eu = led.groupby(["SKU", "region", "Date"], as_index=False).agg(
        on_hand=("Ending Warehouse Balance", "sum"),
        in_transit=("In Transit Between Warehouses", "sum"),
        shipped=("Customer Shipments", "sum"),
        receipts=("Receipts", "sum"),   # genuine inbound (NOT customer returns)
    )
    eu["shipped"] = (-eu["shipped"]).clip(lower=0)  # outbound is negative
    # "Available" stock = on-hand sellable + units in transit between FCs, so a
    # Pan-EU redistribution (units leaving one FC, in transit to another) is not
    # mistaken for a stock drop / stock-out.
    eu["eu_stock"] = eu["on_hand"] + eu["in_transit"]
    eu["SKU"] = eu["SKU"].astype(str)
    return eu


@st.cache_data(show_spinner=False)
def load_discontinued(path: Path | None, mtime: float | None = None) -> pd.DataFrame:
    """Product-discontinuation list (a weekly DB pull) → one row per SKU with the
    date it was delisted.

    A discontinued SKU keeps appearing in the Novadata export for a while, so its
    post-delisting zero-sales tail would otherwise be read as an ongoing
    stock-out / blocked listing (the trailing-zeros safeguard only spares the
    last few weeks before "today"). Cutting a SKU off at its `inactive_date`
    keeps those dead days out of every OOS/cooling/heating/blocked total.

    A later `active_date` (SKU relisted after having been inactive) clears the
    flag — the SKU is live again, not discontinued. `mtime` is part of the cache
    key so an in-place re-upload of the same filename invalidates the cache."""
    cols = ["SKU", "inactive_date", "active_date", "Product"]
    if path is None:
        return pd.DataFrame(columns=cols)
    raw = (pd.read_excel(path) if path.suffix.lower() in (".xlsx", ".xls")
           else pd.read_csv(path))
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    if not {"sku", "product_inactive_date"}.issubset(raw.columns):
        return pd.DataFrame(columns=cols)
    raw["SKU"] = raw["sku"].astype(str).str.strip()
    raw["_ina"] = pd.to_datetime(raw["product_inactive_date"], errors="coerce")
    raw["_act"] = pd.to_datetime(raw.get("product_active_date"), errors="coerce")
    raw["Product"] = raw.get("product_name_short",
                             pd.Series("", index=raw.index)).astype(str)
    # Collapse duplicate/repeat rows to one per SKU: keep the most recent
    # (de)activation dates (the export can carry exact-duplicate rows).
    g = (raw.groupby("SKU", as_index=False)
         .agg(inactive_date=("_ina", "max"), active_date=("_act", "max"),
              Product=("Product", "last")))
    g = g[g["inactive_date"].notna()]
    reactivated = g["active_date"].notna() & (g["active_date"] > g["inactive_date"])
    return g[~reactivated][cols].reset_index(drop=True)


@st.cache_data(show_spinner="Computing stock-out history…")
def compute_oos_long(margin_path: Path, ledger_path: Path | None,
                     m_margin: float | None = None, m_ledger: float | None = None):
    """Build the per-day OOS panel at SKU level, split into two regions.

    Demand and stock are pooled within each region — **EU** (the Pan-EU pool, all
    marketplaces except amazon.co.uk) and **GB** (the separate post-Brexit
    warehouse, amazon.co.uk). Within a region the demand rate (lambda), price,
    CM3 and ad-spend baselines are computed on the pooled totals per SKU. One
    row per (Period, SKU, region).

    Returns (long, meta, asof, eu).
    """
    df = load_margin(margin_path, m_margin)
    # Region split: GB (amazon.co.uk) is its own pool, everything else is EU.
    df["region"] = np.where(
        df["Marketplace Name"].astype(str) == "amazon.co.uk", "GB", "EU")
    eu = load_ledger(ledger_path, m_ledger)
    keys = ["SKU", "region"]

    g = df.groupby(["Period"] + keys, observed=True, as_index=False).agg(
        Units=("Units", "sum"), Sales=("Sales", "sum"),
        CM3=("CM3", "sum"), FBA=("FBA Available", "max"), PPC=("AdSpend", "sum"),
    )
    # The newest export day is an intra-day PARTIAL snapshot (the daily export
    # runs in the morning) — drop it so no rule ever sees a partial day: it
    # would otherwise book phantom zero-sales stock-outs every morning and
    # pollute the demand baselines.
    g = g[g["Period"] < g["Period"].max()]
    full_dates = pd.date_range(g["Period"].min(), g["Period"].max(), freq="D")
    asof = full_dates.max()

    def pivot(col: str, agg: str) -> pd.DataFrame:
        p = g.pivot_table(index="Period", columns=keys, values=col,
                          aggfunc=agg, observed=True)
        return p.reindex(full_dates)

    units = pivot("Units", "sum").fillna(0.0)
    sales = pivot("Sales", "sum").fillna(0.0)
    cm3 = pivot("CM3", "sum").fillna(0.0)
    fba = pivot("FBA", "max")  # NaN where stock unknown
    ppc = pivot("PPC", "sum").fillna(0.0)

    pos = units > 0
    had_past = pos.cummax()
    has_future = pos[::-1].cummax()[::-1]

    # Demand-baseline input = only days the SKU was genuinely live. Two masks:
    # (a) pre-launch days (before the first sale) would dilute lambda for a new
    #     SKU's first ~90 days; (b) LONG zero-runs (>= 7 consecutive zero days —
    #     an outage or blocked listing, not natural sales noise) would make
    #     lambda decay through a stock-out so the biggest outages self-erase.
    # With both masked, an all-masked window yields NaN and the .ffill() below
    # genuinely carries the pre-outage rate forward. Short scattered zeros
    # (< 7 days) still count, so slow movers keep their true low rate.
    z = units.eq(0)
    cz = z.cumsum()
    zrun = cz - cz.where(~z).ffill().fillna(0)   # length of the current zero-run
    units_base = units.where(had_past & ~(z & (zrun >= 7)))
    roll_units = units_base.rolling(BASELINE_WINDOW, min_periods=1).sum()
    roll_days = units_base.rolling(BASELINE_WINDOW, min_periods=1).count()
    roll_sales = sales.rolling(BASELINE_WINDOW, min_periods=1).sum()
    roll_cm3 = cm3.rolling(BASELINE_WINDOW, min_periods=1).sum()
    # Ad-spend baseline = trailing avg over days that actually had spend; NaN
    # before Novadata began reporting Advertising Costs (so no false ad-cuts).
    roll_ppc = ppc.rolling(BASELINE_WINDOW, min_periods=1).sum()
    roll_ppc_days = (ppc > 0).rolling(BASELINE_WINDOW, min_periods=1).sum()
    base_ppc = (roll_ppc / roll_ppc_days.where(roll_ppc_days > 0)).ffill()
    price = sales / units.where(units > 0)  # realised price/unit per day

    # expected = average units per LIVE calendar day = the demand rate.
    # roll_units/roll_days are built from units_base above, which already masks
    # pre-launch days and long (>=7d) zero-runs — so the window only averages
    # days the SKU was genuinely live, and the ffill carries that pre-outage
    # rate forward through a stock-out (the rate can't decay to ~0 and stop
    # flagging OOS). Short scattered zero days still count, so a thin
    # marketplace's normal no-sale days keep it below DEFAULT_MIN_DEMAND and
    # don't look like stock-outs.
    expected = (roll_units / roll_days.where(roll_days > 0)).ffill()
    avg_price = (roll_sales / roll_units.where(roll_units > 0)).ffill()
    avg_cm3_pu = (roll_cm3 / roll_units.where(roll_units > 0)).ffill()

    # Positioned run-rate: average units on recent days the SKU was actively
    # pushed (price cut and/or ad boost — e.g. Prime Day positioning). Remembered
    # for PROMO_WINDOW days (shift(1): the day itself doesn't set its own bar).
    # A price-cut day only counts as positioned if ads weren't simultaneously
    # slashed — discounted-but-ad-cut days are themselves throttled and would
    # dilute the positioned rate. (Before ad data exists, base_ppc is NaN and
    # the ads condition defaults to true.)
    ads_ok = base_ppc.isna() | (ppc >= base_ppc)
    positioned = ((price <= avg_price * (1 - PROMO_PRICE_CUT)) & ads_ok) \
        | (ppc >= base_ppc * PROMO_AD_UP)
    expected_promo = (units.where(positioned)
                      .rolling(PROMO_WINDOW, min_periods=2).mean().shift(1))

    def melt(frame: pd.DataFrame, name: str) -> pd.Series:
        s = frame.stack(["SKU", "region"], future_stack=True)
        s.name = name
        return s

    long = pd.concat(
        [melt(units, "units"), melt(sales, "sales"), melt(cm3, "cm3"),
         melt(fba, "fba"), melt(expected, "expected"),
         melt(expected_promo, "expected_promo"),
         melt(avg_price, "avg_price"), melt(avg_cm3_pu, "avg_cm3_pu"),
         melt(ppc, "ppc"), melt(base_ppc, "base_ppc"), melt(price, "price"),
         melt(had_past, "had_past"), melt(has_future, "has_future")],
        axis=1,
    ).reset_index().rename(columns={"level_0": "Period"})

    long = long[long["had_past"].fillna(False)].copy()
    long["recent"] = long["Period"] >= (asof - pd.Timedelta(days=TAIL_DAYS))
    long["fba_known"] = long["fba"].notna()

    # --- Merge the region's ledger stock + days-of-supply (per SKU × region) ---
    long["SKU"] = long["SKU"].astype(str)
    long["region"] = long["region"].astype(str)
    if not eu.empty:
        e = eu.sort_values("Date").copy()
        e["avgship"] = e.groupby(["SKU", "region"])["shipped"].transform(
            lambda s: s.rolling(28, min_periods=5).mean())
        e["dos"] = e["eu_stock"] / e["avgship"].where(e["avgship"] > 0)
        long = long.merge(e[["SKU", "region", "Date", "eu_stock", "dos", "receipts"]],
                          left_on=["SKU", "region", "Period"],
                          right_on=["SKU", "region", "Date"],
                          how="left").drop(columns=["Date"])
    else:
        long["eu_stock"] = np.nan
        long["dos"] = np.nan
        long["receipts"] = np.nan

    # Carry the last known ledger state forward through days the (manually
    # uploaded) ledger doesn't cover yet — otherwise NaN reach silently
    # disables the blocked/reach rules whenever the ledger is stale (the
    # freshness banner still warns about the staleness itself).
    long = long.sort_values(["SKU", "region", "Period"]).reset_index(drop=True)
    long[["eu_stock", "dos"]] = (
        long.groupby(["SKU", "region"], observed=True)[["eu_stock", "dos"]].ffill())

    for col in ("units", "sales", "cm3", "fba", "expected", "expected_promo",
                "avg_cm3_pu", "avg_price", "ppc", "base_ppc", "price",
                "eu_stock", "dos", "receipts"):
        long[col] = long[col].astype("float32")
    for col in ("SKU", "region"):
        long[col] = long[col].astype("category")

    info = df[["SKU", "Product", "Brand"]].dropna(subset=["SKU"]).copy()
    info["SKU"] = info["SKU"].astype(str)
    meta = info.drop_duplicates(subset=["SKU"], keep="last")
    return long, meta, asof, eu


def flag_oos(long: pd.DataFrame, min_demand: float,
             dos_th: float = COOLDOWN_DOS, ppc_cut: float = COOLDOWN_PPC_CUT,
             price_up: float = COOLDOWN_PRICE_UP,
             min_ppc: float = COOLDOWN_MIN_PPC,
             oos_dos_eu: float = OOS_DOS_EU, oos_dos_gb: float = OOS_DOS_GB,
             heat_ad_up: float = HEATUP_AD_UP, heat_price_down: float = HEATUP_PRICE_DOWN,
             heat_win: int = HEATUP_WINDOW,
             blocked_reach: float = BLOCKED_MIN_REACH) -> pd.DataFrame:
    """Apply the hybrid OOS flag, the cooling-down flag, cause + impact (cheap).

    Categories are mutually exclusive per day, in priority order:
      Physical (network) > Cooling down > Marketplace gap.
    OOS impact (lost_*) is *involuntary* lost sales; cooling-down impact
    (rev_miss / cm3_miss) is the sales we *deliberately* forwent to stretch
    stock — kept separate so the two are never double-counted.
    """
    units = long["units"].to_numpy()
    fba = long["fba"].to_numpy()
    fba_known = long["fba_known"].to_numpy()
    expected = long["expected"].to_numpy()
    had_past = long["had_past"].to_numpy(dtype=bool)
    has_future = long["has_future"].to_numpy(dtype=bool)
    recent = long["recent"].to_numpy(dtype=bool)
    eu_stock = long["eu_stock"].to_numpy()
    ap = long["avg_price"].to_numpy()
    cm3pu = long["avg_cm3_pu"].to_numpy()
    ppc = long["ppc"].to_numpy()
    base_ppc = long["base_ppc"].to_numpy()
    price = long["price"].to_numpy()
    dos = long["dos"].to_numpy()
    # Reach threshold is per region (EU dispatch-to-sellable is faster than GB's
    # transfers + customs), so a combined EU+UK view flags each region on its
    # own bar in the same pass.
    oos_dos = np.where(long["region"].astype(str).to_numpy() == "GB",
                       oos_dos_gb, oos_dos_eu).astype(float)

    # Counterfactual for valuation AND recovery gates: if the SKU was recently
    # POSITIONED to sell faster than usual (promo price/ads, e.g. Prime Day) and
    # that positioned run-rate clearly exceeds lambda, use it — so a day is
    # valued and judged "recovered" against the same bar.
    exp_promo = long["expected_promo"].to_numpy()
    elevated = ~np.isnan(exp_promo) & (exp_promo >= PROMO_ELEV * expected)
    exp_eff = np.where(elevated, exp_promo, expected)

    # Pan-EU: a SKU is physically out of stock only when the whole EU network
    # sellable balance is zero (local-warehouse zeros are served from the pool).
    phys_eu = ~np.isnan(eu_stock) & (eu_stock <= 0) & had_past
    # Reach (days-of-supply) below the OOS threshold = effectively out of stock,
    # even if the balance hasn't hit literally zero yet.
    low_reach = ~np.isnan(dos) & (dos < oos_dos) & had_past

    # Cooling down: a deliberate ad cut and/or price hike while stock is tight
    # (reach in the cool-down band) AND the SKU is still selling. If a throttle
    # pushes sales to zero it isn't "cooling down" — it's an effective stock-out,
    # so units == 0 days fall through to the OOS branches below.
    ad_cut = ~np.isnan(base_ppc) & (base_ppc > min_ppc) & (ppc <= base_ppc * (1 - ppc_cut))
    hike = ~np.isnan(price) & (price >= ap * (1 + price_up))
    cool_stock = ~np.isnan(dos) & (dos < dos_th) & (dos >= oos_dos)
    # An ad cut only signals cooling-down if the price is NOT simultaneously
    # discounted — pulling ads back while still selling below baseline price is
    # a promo winding down (post-heat-up), not a stock-protective throttle.
    not_discounted = ~np.isnan(price) & (price >= ap * 0.98)
    cooldown = (
        ((ad_cut & not_discounted) | hike) & cool_stock & (units > 0)
        & (expected >= min_demand) & had_past & ~phys_eu
    )

    oos_fba = fba_known & (fba == 0) & had_past
    demand_gap = (units == 0) & (expected >= min_demand) & had_past & (has_future | recent)
    # In stock but not selling: zero sales while reach is comfortably high is a
    # LISTING problem (blocked / suppressed / not buyable offer), not a
    # stock-out — there's no Seller Central report for suppression, so this
    # rule is the workaround. Tracked as its own category, excluded from OOS.
    blocked = demand_gap & ~np.isnan(dos) & (dos > blocked_reach)
    oos_gap = demand_gap & ~blocked
    # Involuntary OOS excludes days we chose to throttle (those are cooling-down).
    raw_oos = (phys_eu | low_reach | oos_fba | oos_gap) & ~cooldown & ~blocked

    res = long.copy()
    res["raw_oos"] = raw_oos

    # --- Keep an OOS episode open through return-driven blips ---
    # Customer returns trickle back into the warehouse and can nudge the sellable
    # balance / reach up mid-stock-out, spuriously breaking the OOS run (or even
    # tripping a cooling-down flag). An episode CLOSES when stock demonstrably
    # recovers: a meaningful inbound Receipt (scaled to demand, so several
    # partial deliveries aren't required to arrive on one day) OR reach climbing
    # back above the cool-down band — not on a mere returns blip.
    receipts = long["receipts"].to_numpy()
    restock = ~np.isnan(receipts) & (receipts >= np.maximum(10.0, exp_eff))
    recovered_stock = ~np.isnan(dos) & (dos >= dos_th)
    res["_close"] = restock | recovered_stock
    grp = [res["SKU"].values, res["region"].values]
    last_oos = res["Period"].where(res["raw_oos"]).groupby(grp).ffill()
    last_rs = res["Period"].where(res["_close"]).groupby(grp).ffill()
    oos_open = (last_oos.notna() & (last_rs.isna() | (last_oos > last_rs))).to_numpy()
    # Only bridge days where sales are still suppressed (the OOS symptom) — so a
    # genuine recovery (sales back near the effective rate) ends the episode
    # even before a Receipt.
    filled = oos_open & ~np.isnan(dos) & (units < 0.5 * exp_eff) & had_past & ~blocked
    oos = raw_oos | filled
    cooldown = cooldown & ~oos          # a throttle inside an OOS episode is OOS

    # --- Heating up: the ramp-up after a SKU returns (from a cooling-down or a
    # stock-out). We push ad spend back up and/or drop price (only if we had
    # raised it) to rebuild sales. Two costs: ramp-up lost sales (still below
    # baseline λ) + the extra ad spend. Detected within heat_win days after a
    # disruption (OOS or cooling-down), while back in stock and still ramping.
    disruption = res["Period"].where(oos | cooldown).groupby(grp).ffill()
    since_disrupt = (res["Period"] - disruption).dt.days
    recovery_ctx = (disruption.notna() & (since_disrupt >= 1)
                    & (since_disrupt <= heat_win)).to_numpy()
    last_hike = res["Period"].where(pd.Series(hike, index=res.index)).groupby(grp).ffill()
    prior_hike = (last_hike.notna()
                  & ((res["Period"] - last_hike).dt.days <= heat_win)).to_numpy()
    ad_boost = ~np.isnan(base_ppc) & (base_ppc > min_ppc) & (ppc >= base_ppc * (1 + heat_ad_up))
    price_drop = ~np.isnan(price) & (price <= ap * (1 - heat_price_down)) & prior_hike
    heating = (
        (ad_boost | price_drop) & recovery_ctx & ~oos & ~cooldown & ~phys_eu
        & had_past & (units > 0) & (units < HEATUP_RECOVERED * exp_eff)
    )

    # Cause labels: bridge-filled days can carry positive sales, so they get
    # their own label instead of masquerading as a zero-sales demand gap; no
    # region suffix (GB rows were previously mislabelled "(EU)").
    cause = np.where(
        phys_eu, "Physical (network)",
        np.where(oos & ~np.isnan(dos) & (dos < oos_dos), "Critically low (reach)",
                 np.where(cooldown, "Cooling down",
                          np.where(oos & (units == 0), "Demand gap",
                                   np.where(oos, "Suppressed sales (post-OOS)",
                                            np.where(blocked, "Listing blocked (in stock)",
                                                     np.where(heating, "Heating up", "")))))),
    )

    shortfall = np.clip(exp_eff - units, 0, None)
    lost_units = np.where(oos, shortfall, 0.0)
    miss_units = np.where(cooldown, shortfall, 0.0)
    ramp_units = np.where(heating, shortfall, 0.0)
    extra_ad = np.where(heating, np.clip(ppc - np.nan_to_num(base_ppc), 0, None), 0.0)
    blk_units = np.where(blocked, shortfall, 0.0)
    ap0 = np.nan_to_num(ap)
    cm0 = np.nan_to_num(cm3pu)
    res["oos"] = oos
    res["cooldown"] = cooldown
    res["heating"] = heating
    res["blocked"] = blocked
    res["blk_units"] = blk_units.astype("float32")
    res["blk_rev"] = (blk_units * ap0).astype("float32")
    res["blk_cm3"] = (blk_units * cm0).astype("float32")
    res["cause"] = cause
    res["lost_units"] = lost_units.astype("float32")
    res["lost_rev"] = (lost_units * ap0).astype("float32")
    res["lost_cm3"] = (lost_units * cm0).astype("float32")
    res["miss_units"] = miss_units.astype("float32")
    res["rev_miss"] = (miss_units * ap0).astype("float32")
    res["cm3_miss"] = (miss_units * cm0).astype("float32")
    res["ramp_units"] = ramp_units.astype("float32")
    res["ramp_rev"] = (ramp_units * ap0).astype("float32")
    res["ramp_cm3"] = (ramp_units * cm0).astype("float32")
    res["extra_ad"] = extra_ad.astype("float32")
    return res.drop(columns=["raw_oos", "_close"])



@st.cache_data(show_spinner="Applying thresholds…", max_entries=4)
def compute_flagged(margin_path: Path, ledger_path: Path | None,
                    m_margin: float | None, m_ledger: float | None,
                    min_demand: float, dos_th: float, ppc_cut: float,
                    price_up: float, oos_dos_eu: float, oos_dos_gb: float,
                    heat_ad_up: float,
                    heat_price_down: float, heat_win: int,
                    blocked_reach: float) -> pd.DataFrame:
    """Cached flag_oos: reruns only when data or thresholds actually change,
    instead of re-copying and re-flagging the full panel on every widget
    interaction (search keystrokes, tab sliders, radios)."""
    long_all, _meta, _asof, _eu = compute_oos_long(margin_path, ledger_path,
                                                   m_margin, m_ledger)
    return flag_oos(long_all, min_demand, dos_th=dos_th, ppc_cut=ppc_cut,
                    price_up=price_up, oos_dos_eu=oos_dos_eu, oos_dos_gb=oos_dos_gb,
                    heat_ad_up=heat_ad_up,
                    heat_price_down=heat_price_down, heat_win=heat_win,
                    blocked_reach=blocked_reach)


# ---------- Helpers ----------
def _eu(s: str) -> str:
    """Swap US grouping/decimal to European: 1,234.5 -> 1.234,5."""
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_num(x: float, dec: int = 0) -> str:
    """European-formatted number (dot thousands, comma decimals)."""
    try:
        return _eu(f"{x:,.{dec}f}")
    except (TypeError, ValueError):
        return "—"


def eur(x: float) -> str:
    try:
        return "€" + _eu(f"{x:,.0f}")
    except (TypeError, ValueError):
        return "—"


def short_product(name, n: int = 45) -> str:
    """Readable short product name: the part before the first '|', trimmed."""
    if not isinstance(name, str):
        return ""
    head = name.split("|", 1)[0].strip()
    return head if len(head) <= n else head[: n - 1].rstrip() + "…"


@st.cache_data(show_spinner=False)
def load_english_titles(path: Path) -> pd.Series:
    """SKU -> English product title, from product_titles_en.csv (Shopify export).

    Lets the dashboard show a consistent English name; the Novadata title is
    kept as the fallback for SKUs not present in the Shopify catalog.
    """
    if not path.exists():
        return pd.Series(dtype="object")
    t = pd.read_csv(path, dtype=str).dropna(subset=["SKU", "Title"])
    t["SKU"] = t["SKU"].str.strip()
    return t.drop_duplicates("SKU").set_index("SKU")["Title"]


def stockout_events(scope: pd.DataFrame) -> pd.DataFrame:
    """Collapse consecutive OOS days into events per SKU (EU level)."""
    d = scope[scope["oos"]].copy()
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values(["SKU", "Period"])
    gap = d.groupby("SKU", observed=True)["Period"].diff().dt.days.fillna(1)
    d["event_id"] = (gap > 1).groupby(d["SKU"].values).cumsum().values
    ev = (
        d.groupby(["SKU", "event_id"], observed=True)
        .agg(Start=("Period", "min"), End=("Period", "max"),
             Days=("Period", "count"),
             lost_units=("lost_units", "sum"), lost_rev=("lost_rev", "sum"),
             lost_cm3=("lost_cm3", "sum"),
             cause=("cause", lambda s: s.mode().iat[0] if len(s) else ""))
        .reset_index().drop(columns="event_id")
    )
    return ev


@st.cache_data(show_spinner=False)
def inventory_status(eu: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """Current stock + days-of-supply per SKU × region from the ledger."""
    if eu.empty:
        return pd.DataFrame()
    eu = eu.sort_values("Date")
    last = eu.groupby(["SKU", "region"]).agg(
        cur_stock=("eu_stock", "last"), in_transit=("in_transit", "last"),
        last_date=("Date", "max")).reset_index()
    recent = eu[eu["Date"] >= (eu["Date"].max() - pd.Timedelta(days=30))]
    demand = recent.groupby(["SKU", "region"])["shipped"].mean().rename("avg_daily_demand")
    out = last.merge(demand, on=["SKU", "region"], how="left")
    out["days_of_supply"] = (
        out["cur_stock"] / out["avg_daily_demand"].where(out["avg_daily_demand"] > 0)
    )
    # A SKU whose ledger rows stop well before the ledger's overall end has no
    # current reading — blank it (status shows Unknown) instead of presenting a
    # stale historical balance as "current stock".
    stale = out["last_date"] < (eu["Date"].max() - pd.Timedelta(days=7))
    out.loc[stale, ["cur_stock", "in_transit", "avg_daily_demand", "days_of_supply"]] = np.nan
    return out


def stock_status(cur_stock: pd.Series, reach: pd.Series, oos_reach: float) -> np.ndarray:
    """One status ladder for every table (tabs previously disagreed)."""
    return np.where(
        cur_stock.isna(), "❔ Unknown",
        np.where(cur_stock <= 0, "🔴 Out of stock",
                 np.where(reach.fillna(1e9) < oos_reach, "🔴 Critically low",
                          np.where(reach.fillna(1e9) < LOW_STOCK_DAYS,
                                   "🟠 Low stock", "🟢 In stock"))))


def sku_timeline_fig(d: pd.DataFrame, title: str) -> go.Figure:
    """Methodology-style timeline for one SKU: units/day + expected demand (λ)
    + stock (ledger), with OOS days shaded red and cooling-down days purple."""
    d = d.sort_values("Period")
    fig = go.Figure()
    # Sales (bars) vs the stock level (recessive reference line on a 2nd scale):
    # a deliberate overlay — the whole point of the view is to correlate the
    # stock draw-down with the sales it suppressed.
    fig.add_bar(x=d["Period"], y=d["units"], name="Units sold/day",
                marker_color=brand.CHART_BLUE,
                hovertemplate="%{x|%d %b %Y}<br>%{y:,.0f} units<extra></extra>")
    fig.add_trace(go.Scatter(x=d["Period"], y=d["expected"], name="Expected demand (λ)",
                             mode="lines", line=dict(color=brand.PLUM, dash="dot", width=2),
                             hovertemplate="λ %{y:,.0f}/day<extra></extra>"))
    if d["eu_stock"].notna().any():
        fig.add_trace(go.Scatter(x=d["Period"], y=d["eu_stock"], name="Stock (ledger)",
                                 yaxis="y2", mode="lines",
                                 line=dict(color=brand.PLUM_60, width=1.5),
                                 hovertemplate="%{y:,.0f} in stock<extra></extra>"))
    half = pd.Timedelta(hours=12)
    for dd in d.loc[d["oos"], "Period"]:
        fig.add_vrect(x0=dd - half, x1=dd + half, fillcolor=brand.BAD,
                      opacity=0.12, line_width=0, layer="below")
    for dd in d.loc[d["cooldown"], "Period"]:
        fig.add_vrect(x0=dd - half, x1=dd + half, fillcolor=brand.CHART_VIOLET,
                      opacity=0.12, line_width=0, layer="below")
    fig.update_layout(
        title=title, bargap=0.15,
        yaxis=dict(title="Units / day", rangemode="tozero", tickformat=",.0f"),
        yaxis2=dict(title="Stock", overlaying="y", side="right", showgrid=False,
                    rangemode="tozero", tickformat=",.0f"))
    return brand.style(fig, height=380)
# ======================================================================
require_login()

st.title("📦 OOS Impact Analytics")
st.caption(
    "Tracks Amazon out-of-stock impact over the year — estimated lost revenue "
    "& contribution margin per SKU. Pan-EU: demand (λ) and stock are pooled "
    "EU-wide per SKU, not split by country. Hybrid of the FBA Inventory Ledger "
    "(real stock) and the Novadata margin export (sales & €)."
)

margin_path = latest_export(EXPORTS_DIR)
ledger_path = latest_ledger(LEDGER_DIR)
disc_path = latest_discontinued(EXPORTS_DIR)
if margin_path is None:
    st.error(f"No margin export in `{EXPORTS_DIR}`. Run the Novadata export first.")
    st.stop()

m_margin = margin_path.stat().st_mtime
m_ledger = ledger_path.stat().st_mtime if ledger_path else None
long_all, meta, asof, eu = compute_oos_long(margin_path, ledger_path, m_margin, m_ledger)
prod_map = meta.set_index("SKU")["Product"].astype("object")
en_titles = load_english_titles(REPO_ROOT / "product_titles_en.csv")
if not en_titles.empty:
    prod_map.update(en_titles)             # prefer English where we have it
prod_short = prod_map.map(short_product)   # readable names for tables
brand_map = meta.set_index("SKU")["Brand"]
inv = inventory_status(eu, asof)

# Data-freshness banner: sales (Novadata) refreshes daily via GitHub Actions;
# the Amazon ledger is a manual upload — warn when either is stale.
led_asof = eu["Date"].max() if not eu.empty else None
_today = pd.Timestamp.today().normalize()
_fresh = f"Sales data through **{asof.date()}**"
if led_asof is not None:
    _fresh += f" · stock ledger through **{led_asof.date()}**"
st.caption(_fresh)
_stale = []
if (_today - asof).days > 3:
    _stale.append(f"sales data is {(_today - asof).days} days old (daily refresh "
                  "runs on the default branch)")
if led_asof is not None and (_today - led_asof).days > 7:
    _stale.append(f"stock ledger is {(_today - led_asof).days} days old — upload a "
                  "fresh Inventory Ledger export via add_ledger.py")
if _stale:
    st.warning("⚠️ " + "; ".join(_stale) + ".")

if ledger_path is None:
    st.warning(
        "No Amazon FBA Inventory Ledger found in `amazon_ledger/` — running on "
        "Novadata signals only. Upload a ledger export to enable the physical "
        "stock-out, days-of-supply and risk views. See README_OOS.md."
    )

# ---------- Filter bar ----------
COUNTRY_ALL = "🌍 All countries"


@st.cache_data(show_spinner=False)
def country_alloc(margin_path: Path, m_margin: float | None, region: str) -> pd.DataFrame:
    """Per-SKU × country allocation *shares* from the margin export (full period,
    so a fully-OOS SKU keeps its country mix). Each metric gets its own share so
    the countries always sum back to the SKU's model total:
      • ushare — country's share of the SKU's units  (splits lost units)
      • rshare — country's share of the SKU's revenue (splits lost revenue)
      • cshare — country's share of the SKU's CM3     (splits lost CM3)
    CM3 falls back to the unit share when the SKU's total CM3 isn't positive (so
    a loss-leader SKU doesn't produce nonsensical negative country shares)."""
    mg = load_margin(margin_path, m_margin)
    if region == "EU":
        mg = mg[mg["Marketplace Name"] != "amazon.co.uk"]
    elif region == "GB":
        mg = mg[mg["Marketplace Name"] == "amazon.co.uk"]
    mg = mg.assign(SKU=mg["SKU"].astype(str),
                   Country=mg["Marketplace Name"].map(MKT_COUNTRY).fillna(mg["Marketplace Name"]))
    cm = mg.groupby(["SKU", "Country"], observed=True).agg(
        units=("Units", "sum"), sales=("Sales", "sum"), cm3=("CM3", "sum")).reset_index()
    cm = cm[cm["units"] > 0]
    grp = cm.groupby("SKU")
    tot_u = grp["units"].transform("sum")
    tot_s = grp["sales"].transform("sum")
    tot_c = grp["cm3"].transform("sum")
    cm["ushare"] = cm["units"] / tot_u.where(tot_u > 0)
    cm["rshare"] = cm["sales"] / tot_s.where(tot_s > 0)
    cm["cshare"] = np.where(tot_c > 0, cm["cm3"] / tot_c.where(tot_c > 0), cm["ushare"])
    return cm[["SKU", "Country", "units", "ushare", "rshare", "cshare"]]


REGION_LABEL = {"EU": "🇪🇺 EU (Pan-EU)", "GB": "🇬🇧 GB (UK warehouse)",
                "ALL": "🇪🇺+🇬🇧 EU + UK (combined)"}
_present = [r for r in ["EU", "GB"] if r in set(long_all["region"].unique())]
# Offer the combined view only when both pools are actually present.
regions = _present + (["ALL"] if len(_present) > 1 else [])
c1, cco, c2, c3, c4, c5 = st.columns([1.0, 1.15, 0.8, 1.05, 0.85, 1.1])
region = c1.selectbox("Region", regions, index=0,
                      format_func=lambda r: REGION_LABEL.get(r, r),
                      help="EU = the Pan-EU pool (all marketplaces except "
                           "amazon.co.uk). GB = the separate UK warehouse "
                           "(amazon.co.uk). EU + UK combined sums both pools, "
                           "each still flagged on its own reach threshold.")
# Country filter: scopes the € impact to one country (availability stays
# network-level — see the banner). Options ordered by that country's volume.
_ca = country_alloc(margin_path, m_margin, region)
_country_order = (_ca.groupby("Country")["units"].sum()
                  .sort_values(ascending=False).index.tolist())
country_sel = cco.selectbox(
    "Country", [COUNTRY_ALL] + _country_order,
    help="Splits the € impact (lost / miss / blocked / ramp) onto one country "
         "by each SKU's actual unit-share there, valued at that country's own "
         "price & margin. Stock-outs are Pan-EU network events, so OOS days, "
         "OOS rate, reach and WISR stay network-level (identical across "
         "countries) — only the money is country-specific.")
pmin, pmax = long_all["Period"].min(), asof
ptype = c2.selectbox("Period", ["Full range", "Year", "Quarter", "Month", "Week"], index=0)
sel_periods, sel_freq = None, None
if ptype == "Year":
    opts = list(pd.period_range(pmin, pmax, freq="Y"))[::-1]
    chosen = c3.multiselect("Years", opts, default=[opts[0]],
                            format_func=lambda p: str(p.year))
    sel_freq = "Y"
elif ptype == "Quarter":
    opts = list(pd.period_range(pmin, pmax, freq="Q"))[::-1]
    chosen = c3.multiselect("Quarters", opts, default=[opts[0]],
                            format_func=lambda p: f"{p.year} Q{p.quarter}")
    sel_freq = "Q"
elif ptype == "Month":
    opts = list(pd.period_range(pmin, pmax, freq="M"))[::-1]
    chosen = c3.multiselect("Months", opts, default=[opts[0]],
                            format_func=lambda p: p.strftime("%b %Y"))
    sel_freq = "M"
elif ptype == "Week":
    opts = list(pd.period_range(pmin, pmax, freq="W"))[::-1]
    chosen = c3.multiselect("Weeks", opts, default=[opts[0]],
                            format_func=lambda p: f"CW{p.week} {p.start_time.year}")
    sel_freq = "W"
else:
    c3.multiselect("Bucket", ["— full range —"], default=[], disabled=True)
    chosen = []
if chosen:
    sel_periods = set(chosen)
    start = min(p.start_time for p in chosen)
    end = min(max(p.end_time for p in chosen), pmax)
else:
    start, end = pmin, pmax
min_demand = c4.slider(
    "Min demand (units/day)", 1.0, 10.0, DEFAULT_MIN_DEMAND, 0.5,
    help="A zero-sales day is only treated as a demand-gap stock-out when the "
         "SKU's pooled expected demand rate clears this — high enough that "
         "selling nothing is a real anomaly. Physical (ledger) stock-outs and "
         "critically-low reach always count.",
)
search = c5.text_input("SKU or Product contains", "")

# Region-scoped current-stock lookups for the ranking / status columns.
# Combined view sums stock across both pools and blends reach = total stock /
# total daily demand (a single SKU can sit in both EU and GB).
if inv.empty:
    inv_stock = pd.Series(dtype=float)
    inv_dos = pd.Series(dtype=float)
elif region == "ALL":
    g = inv.groupby("SKU").agg(cur_stock=("cur_stock", "sum"),
                               dmd=("avg_daily_demand", "sum"))
    inv_stock = g["cur_stock"]
    inv_dos = g["cur_stock"] / g["dmd"].where(g["dmd"] > 0)
else:
    inv_r = inv[inv["region"] == region]
    inv_stock = inv_r.set_index("SKU")["cur_stock"] if not inv_r.empty else pd.Series(dtype=float)
    inv_dos = inv_r.set_index("SKU")["days_of_supply"] if not inv_r.empty else pd.Series(dtype=float)

with st.expander("Stock-out, cooling-down & heating-up thresholds"):
    cc1, cc2, cc3, cc4 = st.columns(4)
    _reach_help = ("Reach (days-of-supply) below this counts as out of stock, "
                   "even if the balance isn't literally 0. Defaults per Ops: "
                   "EU 4 (≈ dispatch-to-sellable), GB 12 (longer transfers + "
                   "customs).")
    if region == "ALL":
        # Each pool keeps its own threshold in the combined view.
        rcol1, rcol2 = cc1.columns(2)
        eu_reach = rcol1.slider("OOS reach EU", 1, 21, OOS_DOS_EU, 1,
                                key="oos_reach_EU_combined", help=_reach_help)
        gb_reach = rcol2.slider("OOS reach GB", 1, 21, OOS_DOS_GB, 1,
                                key="oos_reach_GB_combined", help=_reach_help)
        oos_reach = eu_reach  # status-badge tier for blended-stock SKUs
    else:
        _reach_default = OOS_DOS_EU if region == "EU" else OOS_DOS_GB
        oos_reach = cc1.slider("OOS when reach below (days)", 1, 21, _reach_default, 1,
                               key=f"oos_reach_{region}", help=_reach_help)
        eu_reach = oos_reach if region == "EU" else OOS_DOS_EU
        gb_reach = oos_reach if region == "GB" else OOS_DOS_GB
    cd_dos = cc2.slider("Cool-down when reach below (days)", 5, 60, COOLDOWN_DOS, 5)
    cd_price = cc3.slider("Cool-down: price hike vs baseline (%)", 2, 30,
                          int(COOLDOWN_PRICE_UP * 100), 1) / 100
    cd_ppc = cc4.slider("Cool-down: ad-spend cut vs baseline (%)", 20, 90,
                        int(COOLDOWN_PPC_CUT * 100), 5) / 100
    hc1, hc2, hc3, hc4 = st.columns(4)
    heat_ad = hc1.slider("Heat-up: ad-spend up vs baseline (%)", 10, 200,
                         int(HEATUP_AD_UP * 100), 10) / 100
    heat_pr = hc2.slider("Heat-up: price cut vs baseline (%)", 2, 30,
                         int(HEATUP_PRICE_DOWN * 100), 1) / 100
    heat_win = hc3.slider("Heat-up window after return (days)", 7, 60, HEATUP_WINDOW, 7)
    blk_reach = hc4.slider("Blocked listing: reach above (days)", 5, 60, BLOCKED_MIN_REACH, 5,
                           help="Zero-sales days (with demand ≥ the min-demand gate) while "
                                "reach is ABOVE this are tagged 'Listing blocked (in stock)' "
                                "— a listing/offer problem, not a stock-out.")
    st.caption("OOS = reach below the first threshold (or balance 0 / a demand "
               "gap). 'Cooling down' = throttled (price up and/or ad cut) while "
               "stock is tight. 'Heating up' = the ramp-up after a SKU returns — "
               "ad spend pushed up and/or price cut (only if we'd raised it) — "
               "booked as ramp-up lost sales + extra ad spend. Ad-based signals "
               "only apply from when Novadata began reporting Advertising Costs "
               "(~Feb 2026); price signals span the full year.")

# ---------- Apply scope ----------
scope = compute_flagged(margin_path, ledger_path, m_margin, m_ledger,
                        min_demand, cd_dos, cd_ppc, cd_price, eu_reach, gb_reach,
                        heat_ad, heat_pr, heat_win, blk_reach)
if region != "ALL":
    scope = scope[scope["region"] == region]
if sel_periods is not None:
    scope = scope[scope["Period"].dt.to_period(sel_freq).isin(sel_periods)]
else:
    scope = scope[(scope["Period"] >= start) & (scope["Period"] <= end)]
if search.strip():
    s = search.strip().lower()
    cats = scope["SKU"].cat.categories
    hits = [k for k in cats
            if s in str(k).lower() or s in str(prod_map.get(k, "")).lower()]
    scope = scope[scope["SKU"].isin(hits)]

# ---------- Discontinued products (delisted → out of the OOS universe) ----------
# A SKU keeps showing up in the Novadata export after it's delisted; from its
# inactive_date on, those zero-sales days are neither a stock-out nor a blocked
# listing — the product simply isn't sold any more. Drop them from the working
# panel so no involuntary/voluntary/blocked total is inflated by dead SKUs, and
# surface them as their own category below.
disc = load_discontinued(disc_path,
                         disc_path.stat().st_mtime if disc_path else None)
disc_cut = disc.set_index("SKU")["inactive_date"] if not disc.empty else pd.Series(dtype="datetime64[ns]")
if not disc_cut.empty:
    _cut = scope["SKU"].astype(str).map(disc_cut)
    _dmask = _cut.notna().to_numpy() & (scope["Period"] >= _cut).to_numpy()
    disc_scope = scope[_dmask]        # delisted SKU-days that fall in the view
    scope = scope[~_dmask]
else:
    disc_scope = scope.iloc[:0]
if scope.empty:
    st.warning("No data in the current scope. Widen the filters.")
    st.stop()

# ---------- Country allocation (rescope € impact to one country) ----------
# Stock is pooled Pan-EU, so a stock-out is one network event — the OOS flags,
# days, reach and WISR are network-level and identical for every country. Only
# the *money* is country-specific: split each SKU's lost/miss/blocked/ramp units
# by its unit-share in the chosen country, valued at that country's price & CM3.
if country_sel != COUNTRY_ALL:
    _sub = _ca[_ca["Country"] == country_sel].set_index("SKU")
    _sk = scope["SKU"].astype(str)
    _us = _sk.map(_sub["ushare"]).fillna(0.0).to_numpy()   # units
    _rs = _sk.map(_sub["rshare"]).fillna(0.0).to_numpy()   # revenue
    _cs = _sk.map(_sub["cshare"]).fillna(0.0).to_numpy()   # CM3
    scope = scope.copy()
    # Each metric split by its own country share → countries sum to the total.
    for _col, _w in [("lost_units", _us), ("lost_rev", _rs), ("lost_cm3", _cs),
                     ("miss_units", _us), ("rev_miss", _rs), ("cm3_miss", _cs),
                     ("blk_units", _us), ("blk_rev", _rs), ("blk_cm3", _cs),
                     ("ramp_units", _us), ("ramp_rev", _rs), ("ramp_cm3", _cs),
                     ("extra_ad", _us)]:
        scope[_col] = (scope[_col].to_numpy() * _w).astype("float32")
    st.info(
        f"💶 **€ impact allocated to {country_sel}** — lost / miss / blocked / "
        "ramp values below are this country's share of each SKU (units by unit "
        "share, revenue by revenue share, CM3 by CM3 share), so the countries "
        "sum back to the Pan-EU total. **Availability — OOS rate, WISR, reach, "
        "the calendar and events — is network-level and identical across "
        "countries** (stock-outs hit the whole pool at once).")

# ---------- Per-SKU aggregation ----------
oos_rows = scope[scope["oos"]]
agg = (
    oos_rows.groupby("SKU", observed=True)
    .agg(oos_days=("Period", "nunique"), lost_units=("lost_units", "sum"),
         lost_rev=("lost_rev", "sum"), lost_cm3=("lost_cm3", "sum"))
    .reset_index()
)
active = scope.groupby("SKU", observed=True)["Period"].nunique().rename("active_days")
agg = agg.merge(active, on="SKU", how="left")
agg["oos_rate"] = (agg["oos_days"] / agg["active_days"] * 100).round(1)
agg["Product"] = agg["SKU"].map(prod_short)
agg["Brand"] = agg["SKU"].map(brand_map)
agg["cur_stock"] = agg["SKU"].map(inv_stock)
agg["days_of_supply"] = agg["SKU"].map(inv_dos).round(0)
agg["Status"] = stock_status(agg["cur_stock"], agg["days_of_supply"], oos_reach)
agg = agg.sort_values("lost_cm3", ascending=False)

# ---------- Header: OOS impact over time (hero) + split KPI groups ----------
cd_rows = scope[scope["cooldown"]]
heat_rows = scope[scope["heating"]]

st.markdown("### 🔴 Out-of-stock impact")
ovb = st.radio("Bucket", ["Month", "Quarter"], horizontal=True, key="ov_bucket")
operf = "M" if ovb == "Month" else "Q"
sc = scope[["Period", "SKU", "oos", "lost_rev", "lost_cm3",
            "expected", "avg_price"]].copy()
sc["bucket"] = sc["Period"].dt.to_period(operf).dt.to_timestamp()
# WISR weight = expected revenue/day (λ × avg price) — a stable base a stock-out
# can't shrink, unlike trailing realized revenue.
sc["_w"] = sc["expected"] * sc["avg_price"].fillna(0)
sc["_w_in"] = sc["_w"] * (~sc["oos"])
ob = sc.groupby("bucket", observed=True).agg(
    lost_rev=("lost_rev", "sum"), lost_cm3=("lost_cm3", "sum"),
    oos_days=("oos", "sum"), active=("Period", "count"),
    w=("_w", "sum"), w_in=("_w_in", "sum")).reset_index().sort_values("bucket")
ob["rate"] = (ob["oos_days"] / ob["active"].where(ob["active"] > 0) * 100).round(1)
ob["wisr"] = (ob["w_in"] / ob["w"].where(ob["w"] > 0) * 100).round(1)
ob["label"] = (ob["bucket"].dt.strftime("%b %Y") if operf == "M"
               else ob["bucket"].dt.to_period("Q").astype(str))
# Two measures on different scales (€ and %) → two stacked panels sharing the
# x-axis, never one dual-axis chart.
figO = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.09,
                     row_heights=[0.62, 0.38])
figO.add_bar(x=ob["label"], y=ob["lost_cm3"], name="Lost CM3 (€)",
             marker_color=brand.CHART_ORANGE,
             hovertemplate="%{x}<br>Lost CM3 €%{y:,.0f}<extra></extra>", row=1, col=1)
figO.add_bar(x=ob["label"], y=ob["lost_rev"], name="Lost revenue (€)",
             marker_color=brand.CHART_BLUE,
             hovertemplate="%{x}<br>Lost revenue €%{y:,.0f}<extra></extra>", row=1, col=1)
figO.add_trace(go.Scatter(x=ob["label"], y=ob["rate"], name="OOS rate (%)",
                          mode="lines+markers", line=dict(color=brand.CHART_ORANGE, width=2),
                          marker=dict(size=7),
                          hovertemplate="%{x}<br>OOS rate %{y:,.1f} %<extra></extra>"),
               row=2, col=1)
figO.add_trace(go.Scatter(x=ob["label"], y=ob["wisr"], name="WISR (%)",
                          mode="lines+markers", line=dict(color=brand.CHART_BLUE, width=2, dash="dot"),
                          marker=dict(size=7),
                          hovertemplate="%{x}<br>WISR %{y:,.1f} %<extra></extra>"),
               row=2, col=1)
figO.update_layout(barmode="group", bargap=0.28, bargroupgap=0.12,
                   title="Lost impact, OOS rate & WISR over time")
figO.update_yaxes(title_text="€ lost", tickprefix="€ ", tickformat=",.0f", row=1, col=1)
figO.update_yaxes(title_text="rate", ticksuffix=" %", tickformat=",.0f",
                  rangemode="tozero", row=2, col=1)
figO.update_xaxes(type="category", row=2, col=1)
st.plotly_chart(brand.style(figO, height=440), width="stretch")
# Involuntary loss totals, beneath the chart.
o1, o2, o3, o4, o5 = st.columns(5)
o1.metric("SKUs affected", fmt_num(agg["SKU"].nunique()))
o2.metric("Lost revenue", eur(agg["lost_rev"].sum()))
o3.metric("Lost CM3 (P&L impact)", eur(agg["lost_cm3"].sum()))
_rate = len(oos_rows) / max(len(scope), 1) * 100
o4.metric("OOS rate", f"{_rate:.1f}".replace(".", ",") + " %")
_wtot, _wintot = sc["_w"].sum(), sc["_w_in"].sum()
_wisr = _wintot / _wtot * 100 if _wtot > 0 else float("nan")
o5.metric("WISR", f"{_wisr:.1f}".replace(".", ",") + " %",
          help="Weighted In-Stock Rate: % of time in stock, weighted by each "
               "SKU's expected revenue (demand rate λ × avg price) — high-value "
               "SKUs dominate the score, and a stock-out can't shrink its own weight.")

st.markdown("**🟣 Cooling down — voluntary throttle (miss)**")
c1, c2, c3, c4 = st.columns(4)
c1.metric("SKUs cooled down", fmt_num(cd_rows["SKU"].nunique()))
c2.metric("Miss revenue", eur(cd_rows["rev_miss"].sum()))
c3.metric("Miss CM3", eur(cd_rows["cm3_miss"].sum()))
c4.metric("Cool-down SKU-days", fmt_num(len(cd_rows)))

st.markdown("**🔥 Heating up — ramp-up after return (lost sales + extra ad spend)**")
h1, h2, h3, h4 = st.columns(4)
h1.metric("SKUs heating up", fmt_num(heat_rows["SKU"].nunique()))
h2.metric("Ramp-up lost CM3", eur(heat_rows["ramp_cm3"].sum()))
h3.metric("Extra ad spend", eur(heat_rows["extra_ad"].sum()))
h4.metric("Heat-up SKU-days", fmt_num(len(heat_rows)))

blk_rows = scope[scope["blocked"]]
st.markdown("**🚫 Listing blocked — in stock but not selling (not counted as OOS)**")
b1, b2, b3, b4 = st.columns(4)
b1.metric("SKUs affected", fmt_num(blk_rows["SKU"].nunique()))
b2.metric("Blocked SKU-days", fmt_num(len(blk_rows)))
b3.metric("Unrealized revenue", eur(blk_rows["blk_rev"].sum()))
b4.metric("Unrealized CM3", eur(blk_rows["blk_cm3"].sum()))
if not blk_rows.empty:
    with st.expander("Blocked-listing SKUs (check the offer/listing!)"):
        bagg = (blk_rows.groupby("SKU", observed=True)
                .agg(days=("Period", "nunique"), last=("Period", "max"),
                     rev=("blk_rev", "sum"), cm3=("blk_cm3", "sum"))
                .reset_index().sort_values("cm3", ascending=False))
        bagg["Product"] = bagg["SKU"].map(prod_short)
        bagg["last"] = bagg["last"].dt.date
        bdisp = bagg[["SKU", "Product", "days", "last", "rev", "cm3"]].rename(columns={
            "days": "Blocked days", "last": "Last blocked day",
            "rev": "Unrealized revenue (€)", "cm3": "Unrealized CM3 (€)"}).round(0)
        st.dataframe(bdisp, width="stretch", hide_index=True, height=300,
                     column_config={
                         "Unrealized revenue (€)": st.column_config.NumberColumn(format="localized"),
                         "Unrealized CM3 (€)": st.column_config.NumberColumn(format="localized")})

delisted_in_period = disc[(disc["inactive_date"] >= start)
                          & (disc["inactive_date"] <= end)] if not disc.empty else disc
st.markdown("**🗑️ Discontinued — delisted products (removed from every total above)**")
g1, g2, g3, g4 = st.columns(4)
g1.metric("Delisted SKUs in view", fmt_num(disc_scope["SKU"].nunique()))
g2.metric("Dead SKU-days removed", fmt_num(len(disc_scope)),
          help="Post-delisting zero-sales days dropped from the OOS/blocked "
               "universe so they don't masquerade as stock-outs.")
g3.metric("Delisted this period", fmt_num(len(delisted_in_period)),
          help="Products whose delisting date falls inside the selected period.")
g4.metric("On the discontinued list", fmt_num(len(disc)))
if disc_path is None:
    st.caption("No discontinued-products list found in `novadata_exports/` "
               "(`*discontinued*.xlsx`/`.csv`). Drop the weekly DB pull there to "
               "keep delisted SKUs out of the OOS totals.")
elif not delisted_in_period.empty:
    with st.expander(f"Products delisted in this period ({len(delisted_in_period)})"):
        dt = delisted_in_period.copy()
        dt["Product"] = dt["Product"].where(dt["Product"].astype(bool),
                                             dt["SKU"].map(prod_short))
        dt["active_date"] = dt["active_date"].dt.date
        dt["inactive_date"] = dt["inactive_date"].dt.date
        ddisp = (dt[["SKU", "Product", "active_date", "inactive_date"]]
                 .sort_values("inactive_date", ascending=False)
                 .rename(columns={"active_date": "Active since",
                                  "inactive_date": "Delisted on"}))
        st.dataframe(ddisp, width="stretch", hide_index=True, height=300)

st.caption(
    f"Data through **{asof.date()}** · scope **{start.date()} → {end.date()}** "
    f"· region **{REGION_LABEL.get(region, region)}**. *Lost* = sales forfeited while out of stock; "
    "*Miss* = sales given up by deliberately throttling to avoid OOS; *Ramp-up "
    "lost + extra ad spend* = the cost of bringing a SKU back after it returns; "
    "*Blocked* = zero sales despite healthy stock (reach above the blocked "
    "threshold) — a listing/offer issue, kept out of the OOS totals; "
    "*Discontinued* = delisted products (per the weekly list), dropped from "
    "every total. All valued as CM3 / € — the true P&L impact."
)

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    ["Most affected SKUs", "Stock-out calendar", "Stock-out events",
     "Cooling down", "Heating up", "Country overview", "Top sellers"]
)

# ======================================================================
#  Tab 1 — Most affected SKUs
# ======================================================================
with tab1:
    top_n = st.slider("Show top N SKUs by lost CM3", 5, 50, 15, key="topn")
    top = agg.head(top_n)
    fig = px.bar(
        top.sort_values("lost_cm3"), x="lost_cm3", y="SKU", orientation="h",
        hover_data={"Product": True, "lost_rev": ":,.0f", "oos_days": True},
        labels={"lost_cm3": "Lost CM3 (€)", "SKU": ""},
        title=f"Top {top_n} SKUs by lost contribution margin",
    )
    fig.update_traces(marker_color=brand.CHART_ORANGE)
    fig.update_xaxes(tickprefix="€ ", tickformat=",.0f")
    st.plotly_chart(brand.style(fig, height=max(340, 30 * top_n), legend=False),
                    width="stretch")

    # Drill-down: pick one of the top SKUs → stock / demand / OOS timeline.
    sel = st.selectbox(
        "Show timeline for a top SKU", top["SKU"].tolist(),
        format_func=lambda s: f"{s} · {prod_short.get(s, '')}", key="t1_sku")
    legend = ("🔴 red = out of stock · 🟣 purple = cooling down · "
              "dotted = expected demand (λ) · grey = stock")
    st.caption(legend)
    st.plotly_chart(
        sku_timeline_fig(scope[scope["SKU"] == sel],
                         f"{sel} — {prod_short.get(sel, '')} ({REGION_LABEL.get(region, region)})"),
        width="stretch")

    table = agg[[
        "SKU", "Product", "Brand", "Status", "oos_days", "oos_rate",
        "lost_units", "lost_rev", "lost_cm3", "cur_stock", "days_of_supply",
    ]].rename(columns={
        "oos_days": "OOS days", "oos_rate": "OOS rate %", "lost_units": "Lost units",
        "lost_rev": "Lost revenue (€)", "lost_cm3": "Lost CM3 (€)",
        "cur_stock": "Current FBA stock", "days_of_supply": "Days of supply",
    })
    table = table.round(0)
    st.dataframe(
        table, width="stretch", hide_index=True, height=520,
        column_config={
            "Lost revenue (€)": st.column_config.NumberColumn(format="localized"),
            "Lost CM3 (€)": st.column_config.NumberColumn(format="localized"),
            "Lost units": st.column_config.NumberColumn(format="localized"),
            "Current FBA stock": st.column_config.NumberColumn(format="localized"),
            "Days of supply": st.column_config.NumberColumn(format="localized"),
            "OOS rate %": st.column_config.ProgressColumn(
                format="%.0f%%", min_value=0, max_value=100),
        },
    )
    st.download_button(
        "⬇️ Download ranking (CSV)", table.to_csv(index=False).encode("utf-8"),
        file_name=f"oos_ranking_{start.date()}_{end.date()}.csv", mime="text/csv")

# ======================================================================
#  Tab 2 — Impact over time
# ======================================================================
with tab2:
    freq_label = st.radio("Bucket", ["Month", "Quarter"], horizontal=True, key="cal_bucket")
    per = "M" if freq_label == "Month" else "Q"
    ts = oos_rows.copy()
    ts["bucket"] = ts["Period"].dt.to_period(per).dt.to_timestamp()

    st.subheader("Stock-out calendar — top SKUs")
    n_heat = st.slider("SKUs in heatmap", 5, 40, 15, key="heat")
    top_skus = agg.head(n_heat)["SKU"].tolist()
    hm = ts[ts["SKU"].isin(top_skus)]
    if not hm.empty:
        pv = hm.pivot_table(index="SKU", columns="bucket", values="Period",
                            aggfunc="count", observed=True).reindex(top_skus)
        pv.columns = [c.strftime("%b %y") for c in pv.columns]
        pv.index = [f"{s} · {prod_short.get(s, '')}"[:32] for s in pv.index]
        figh = px.imshow(pv, color_continuous_scale=brand.SEQUENTIAL, aspect="auto",
                         labels=dict(color="OOS days"))
        figh.update_traces(hovertemplate="%{y}<br>%{x}<br>%{z:,.0f} OOS days<extra></extra>",
                           xgap=2, ygap=2)
        figh.update_xaxes(showgrid=False)
        figh.update_yaxes(showgrid=False)
        st.plotly_chart(brand.style(figh, height=max(320, 28 * n_heat), legend=False),
                        width="stretch")

# ======================================================================
#  Tab 3 — Stock-out events
# ======================================================================
with tab3:
    ev = stockout_events(scope)
    if ev.empty:
        st.info("No stock-out events in the current scope.")
    else:
        ev["Product"] = ev["SKU"].map(prod_short)
        ev = ev.sort_values("lost_cm3", ascending=False)
        disp = ev[["SKU", "Product", "cause", "Start", "End",
                   "Days", "lost_units", "lost_rev", "lost_cm3"]].rename(columns={
            "cause": "Cause",
            "lost_units": "Lost units", "lost_rev": "Lost revenue (€)",
            "lost_cm3": "Lost CM3 (€)"})
        disp["Start"] = disp["Start"].dt.date
        disp["End"] = disp["End"].dt.date
        disp = disp.round(0)
        st.caption(f"{fmt_num(len(disp))} discrete stock-out events · "
                   f"longest {int(ev['Days'].max())} days.")
        st.dataframe(
            disp, width="stretch", hide_index=True, height=520,
            column_config={
                "Lost revenue (€)": st.column_config.NumberColumn(format="localized"),
                "Lost CM3 (€)": st.column_config.NumberColumn(format="localized"),
                "Lost units": st.column_config.NumberColumn(format="localized")})
        st.download_button(
            "⬇️ Download events (CSV)", disp.to_csv(index=False).encode("utf-8"),
            file_name=f"oos_events_{start.date()}_{end.date()}.csv", mime="text/csv")

# ======================================================================
#  Tab 4 — Cooling down (deliberate demand throttling to avoid OOS)
# ======================================================================
with tab4:
    st.caption(
        "Days where the SKU was **deliberately throttled** — ad spend cut and/or "
        "price raised — while stock was tight, to glide to the next shipment "
        "instead of hard stocking out (OOS hurts Amazon ranking). The **miss** is "
        "the sales/margin voluntarily forgone (expected demand − actual units, "
        "valued at the normal price / CM3). Separate from involuntary OOS loss."
    )
    cd = scope[scope["cooldown"]]
    if cd.empty:
        st.info("No cooling-down days detected in the current scope. Loosen the "
                "thresholds in 'Cooling-down detection settings' above.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("SKUs cooled down", fmt_num(cd["SKU"].nunique()))
        m2.metric("Revenue miss", eur(cd["rev_miss"].sum()))
        m3.metric("CM3 miss", eur(cd["cm3_miss"].sum()))
        m4.metric("Cool-down SKU-days", fmt_num(len(cd)))

        cagg = (
            cd.groupby("SKU", observed=True)
            .agg(cool_days=("Period", "nunique"), miss_units=("miss_units", "sum"),
                 rev_miss=("rev_miss", "sum"), cm3_miss=("cm3_miss", "sum"))
            .reset_index().sort_values("cm3_miss", ascending=False)
        )
        cagg["Product"] = cagg["SKU"].map(prod_short)
        table = cagg[["SKU", "Product", "cool_days", "miss_units",
                      "rev_miss", "cm3_miss"]].rename(columns={
            "cool_days": "Cool-down days", "miss_units": "Units miss",
            "rev_miss": "Revenue miss (€)", "cm3_miss": "CM3 miss (€)"})
        table = table.round(0)
        st.dataframe(
            table, width="stretch", hide_index=True, height=460,
            column_config={
                "Revenue miss (€)": st.column_config.NumberColumn(format="localized"),
                "CM3 miss (€)": st.column_config.NumberColumn(format="localized"),
                "Units miss": st.column_config.NumberColumn(format="localized")})
        st.download_button(
            "⬇️ Download cooling-down (CSV)", table.to_csv(index=False).encode("utf-8"),
            file_name=f"oos_cooldown_{start.date()}_{end.date()}.csv", mime="text/csv")
        st.caption(
            "Note: ad-spend-cut detection only applies from when Novadata began "
            "reporting Advertising Costs (~Feb 2026); price-hike detection spans "
            "the full year. Tune sensitivity in the settings expander above.")

# ======================================================================
#  Tab 5 — Heating up (ramp-up after a SKU returns)
# ======================================================================
with tab5:
    st.caption(
        "The **ramp-up after a SKU returns** (from a cooling-down or a stock-out): "
        "we push ad spend back up and/or cut price (only if we'd raised it) to "
        "rebuild momentum. Two costs: **ramp-up lost sales** (sales still below "
        "baseline λ while recovering) and the **extra ad spend** vs the normal "
        "baseline. A stock-out isn't required — heat-up can follow a cooling-down "
        "directly."
    )
    hu = scope[scope["heating"]]
    if hu.empty:
        st.info("No heating-up days detected in the current scope. Loosen the "
                "heat-up thresholds in the settings expander above.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("SKUs heating up", fmt_num(hu["SKU"].nunique()))
        m2.metric("Ramp-up lost revenue", eur(hu["ramp_rev"].sum()))
        m3.metric("Ramp-up lost CM3", eur(hu["ramp_cm3"].sum()))
        m4.metric("Extra ad spend", eur(hu["extra_ad"].sum()))

        hagg = (
            hu.groupby("SKU", observed=True)
            .agg(heat_days=("Period", "nunique"), ramp_units=("ramp_units", "sum"),
                 ramp_rev=("ramp_rev", "sum"), ramp_cm3=("ramp_cm3", "sum"),
                 extra_ad=("extra_ad", "sum"))
            .reset_index().sort_values("ramp_cm3", ascending=False)
        )
        hagg["Product"] = hagg["SKU"].map(prod_short)
        table = hagg[["SKU", "Product", "heat_days", "ramp_units",
                      "ramp_rev", "ramp_cm3", "extra_ad"]].rename(columns={
            "heat_days": "Heat-up days", "ramp_units": "Ramp-up lost units",
            "ramp_rev": "Ramp-up lost rev (€)", "ramp_cm3": "Ramp-up lost CM3 (€)",
            "extra_ad": "Extra ad spend (€)"})
        table = table.round(0)
        st.dataframe(
            table, width="stretch", hide_index=True, height=460,
            column_config={
                "Ramp-up lost rev (€)": st.column_config.NumberColumn(format="localized"),
                "Ramp-up lost CM3 (€)": st.column_config.NumberColumn(format="localized"),
                "Ramp-up lost units": st.column_config.NumberColumn(format="localized"),
                "Extra ad spend (€)": st.column_config.NumberColumn(format="localized")})
        st.download_button(
            "⬇️ Download heating-up (CSV)", table.to_csv(index=False).encode("utf-8"),
            file_name=f"oos_heatup_{start.date()}_{end.date()}.csv", mime="text/csv")
        st.caption(
            "Thresholds are provisional (defaults: ad +50%, price −10%, "
            "28-day window) — pending Logistics/Ops input. Ad-spend signals need "
            "Advertising Costs data (~Feb 2026 onward).")

# ======================================================================
#  Tab 6 — Country overview (allocate Pan-EU OOS loss to countries)
# ======================================================================
with tab6:
    if country_sel != COUNTRY_ALL:
        st.info(
            f"A country is selected in the top filter (**{country_sel}**), so the "
            "whole dashboard is already scoped to it — the KPIs and every tab show "
            f"{country_sel}'s allocated impact. Set **Country → 🌍 All countries** "
            "to compare across countries here.")
    else:
        st.caption(
            "OOS impact per country — each SKU's lost units / revenue / CM3 split by "
            "its **share of that country** (units by unit share, revenue by revenue "
            "share, CM3 by CM3 share), so the countries **sum back to the Pan-EU "
            "total** above. Same stock-out, but a high-margin country (DE) carries "
            "more of the € than a thin one (ES). Pick a country to drill into its SKUs."
        )
        # Full-period country shares (a fully-OOS SKU keeps its mix); losses (agg)
        # stay window-scoped. `_ca` is the cached per-SKU × country share table
        # (ushare / rshare / cshare) also used by the top-level Country filter.
        alloc = _ca.merge(agg[["SKU", "lost_units", "lost_rev", "lost_cm3"]],
                          on="SKU", how="inner")
        alloc["units_lost"] = alloc["lost_units"] * alloc["ushare"]
        alloc["c_rev"] = alloc["lost_rev"] * alloc["rshare"]
        alloc["c_cm3"] = alloc["lost_cm3"] * alloc["cshare"]

        if alloc.empty:
            st.info("No allocatable OOS loss in the current scope.")
        else:
            country = (alloc.groupby("Country", observed=True).agg(
                units_lost=("units_lost", "sum"), lost_rev=("c_rev", "sum"),
                lost_cm3=("c_cm3", "sum"), oos_skus=("SKU", "nunique"))
                .reset_index().sort_values("lost_cm3", ascending=False))
            pick = st.selectbox("Country", ["🌍 All countries"] + country["Country"].tolist())

            if pick == "🌍 All countries":
                k1, k2, k3 = st.columns(3)
                k1.metric("Countries", fmt_num(len(country)))
                k2.metric("Lost revenue", eur(country["lost_rev"].sum()))
                k3.metric("Lost CM3", eur(country["lost_cm3"].sum()))
                figc = px.bar(country.sort_values("lost_cm3"), x="lost_cm3", y="Country",
                              orientation="h", labels={"lost_cm3": "Lost CM3 (€)", "Country": ""},
                              title="OOS lost CM3 by country (share of the Pan-EU total)")
                figc.update_traces(marker_color=brand.CHART_ORANGE)
                figc.update_xaxes(tickprefix="€ ", tickformat=",.0f")
                st.plotly_chart(brand.style(figc, height=max(320, 44 * len(country)),
                                            legend=False), width="stretch")
                disp = country[["Country", "oos_skus", "units_lost", "lost_rev", "lost_cm3"]].rename(
                    columns={"oos_skus": "OOS SKUs", "units_lost": "Lost units",
                             "lost_rev": "Lost revenue (€)", "lost_cm3": "Lost CM3 (€)"}).round(0)
                fname = f"oos_by_country_{start.date()}_{end.date()}.csv"
            else:
                d = alloc[alloc["Country"] == pick].copy()
                d["Product"] = d["SKU"].map(prod_short)
                k1, k2, k3 = st.columns(3)
                k1.metric("SKUs affected", fmt_num(d["SKU"].nunique()))
                k2.metric("Lost revenue", eur(d["c_rev"].sum()))
                k3.metric("Lost CM3", eur(d["c_cm3"].sum()))
                disp = d[["SKU", "Product", "units_lost", "c_rev", "c_cm3"]].rename(
                    columns={"units_lost": "Lost units", "c_rev": "Lost revenue (€)",
                             "c_cm3": "Lost CM3 (€)"}).sort_values("Lost CM3 (€)", ascending=False).round(0)
                fname = f"oos_{pick.split()[-1]}_{start.date()}_{end.date()}.csv"

            st.dataframe(
                disp, width="stretch", hide_index=True, height=420,
                column_config={
                    "Lost revenue (€)": st.column_config.NumberColumn(format="localized"),
                    "Lost CM3 (€)": st.column_config.NumberColumn(format="localized"),
                    "Lost units": st.column_config.NumberColumn(format="localized")})
            st.download_button("⬇️ Download (CSV)", disp.to_csv(index=False).encode("utf-8"),
                               file_name=fname, mime="text/csv")
            st.caption("Lost € per country = each SKU's model lost units / revenue / CM3 × "
                       "that country's share of the SKU (unit / revenue / CM3 share "
                       "respectively, full-period), so the country totals reconcile to "
                       "the Pan-EU total shown in the header.")

    # ======================================================================
    #  Tab 7 — Top sellers (OOS tracker for the highest-value SKUs)
    # ======================================================================
with tab7:
    st.caption(
        "Our **highest-value SKUs** ranked by expected revenue (demand rate λ × "
        "avg price — a stable base a stock-out can't shrink, unlike trailing "
        "realized revenue) and how availability treated them in the selected "
        "period. This is the watchlist: a stock-out here hurts most."
    )
    tb = scope[["SKU", "Period", "expected", "avg_price", "oos",
                "lost_rev", "lost_cm3"]].copy()
    tb["_exp_rev"] = tb["expected"] * tb["avg_price"].fillna(0)
    ts_n = st.slider("Top N by expected revenue", 10, 50, 20, 5, key="ts_n")
    t = tb.groupby("SKU", observed=True).agg(
        exp_rev=("_exp_rev", "mean"), oos_days=("oos", "sum"),
        active=("Period", "nunique"), lost_rev=("lost_rev", "sum"),
        lost_cm3=("lost_cm3", "sum")).reset_index()
    t = t.sort_values("exp_rev", ascending=False).head(ts_n)
    t["oos_rate"] = (t["oos_days"] / t["active"].where(t["active"] > 0) * 100).round(1)
    t["Product"] = t["SKU"].map(prod_short)
    t["cur_stock"] = t["SKU"].map(inv_stock)
    t["reach"] = t["SKU"].map(inv_dos).round(0)
    t["Status"] = stock_status(t["cur_stock"], t["reach"], oos_reach)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Top sellers with OOS days", fmt_num(int((t["oos_days"] > 0).sum())))
    k2.metric("Lost revenue (top sellers)", eur(t["lost_rev"].sum()))
    k3.metric("Lost CM3 (top sellers)", eur(t["lost_cm3"].sum()))
    k4.metric("At risk right now", fmt_num(int(t["Status"].isin(
        ["🔴 Out of stock", "🔴 Critically low"]).sum())))
    disp = t[["SKU", "Product", "Status", "exp_rev", "oos_days", "oos_rate",
              "lost_rev", "lost_cm3", "cur_stock", "reach"]].rename(columns={
        "exp_rev": "Expected €/day", "oos_days": "OOS days", "oos_rate": "OOS rate %",
        "lost_rev": "Lost revenue (€)", "lost_cm3": "Lost CM3 (€)",
        "cur_stock": "Available stock", "reach": "Reach (days)"}).round(0)
    st.dataframe(
        disp, width="stretch", hide_index=True, height=560,
        column_config={
            "Expected €/day": st.column_config.NumberColumn(format="localized"),
            "Lost revenue (€)": st.column_config.NumberColumn(format="localized"),
            "Lost CM3 (€)": st.column_config.NumberColumn(format="localized"),
            "Available stock": st.column_config.NumberColumn(format="localized"),
            "Reach (days)": st.column_config.NumberColumn(format="localized"),
            "OOS rate %": st.column_config.ProgressColumn(
                format="%.0f%%", min_value=0, max_value=100),
        })
    st.download_button(
        "⬇️ Download top sellers (CSV)", disp.to_csv(index=False).encode("utf-8"),
        file_name=f"oos_topsellers_{start.date()}_{end.date()}.csv", mime="text/csv")
