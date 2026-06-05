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
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = REPO_ROOT / "novadata_exports"
LEDGER_DIR = REPO_ROOT / "amazon_ledger"

# Demand baseline window + OOS heuristic defaults.
BASELINE_WINDOW = 90       # trailing window for expected demand / price / CM3
DEFAULT_MIN_DEMAND = 3.0   # min expected units/day to infer a marketplace gap
TAIL_DAYS = 21             # treat trailing zero-runs near "today" as ongoing OOS
LOW_STOCK_DAYS = 21        # days-of-supply threshold for the low-stock risk view
OOS_DOS = 3                # reach (days-of-supply) below this counts as OOS

# "Cooling down" = deliberately throttling demand (cutting PPC and/or raising
# price) while stock is tight, to glide to the next shipment instead of hard
# stocking out (OOS hurts Amazon ranking). Defaults for detecting it:
COOLDOWN_DOS = 30          # only when days-of-supply is at/below this (tight)
COOLDOWN_PPC_CUT = 0.5     # ad spend <= this fraction of baseline = an ad cut
COOLDOWN_PRICE_UP = 0.08   # price >= baseline x (1+this) = a deliberate hike
COOLDOWN_MIN_PPC = 2.0     # ignore SKUs whose baseline ad spend < this (EUR/day)

st.set_page_config(page_title="OOS Impact Analytics", page_icon="📦", layout="wide")


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


# ---------- Loaders ----------
@st.cache_data(show_spinner=False)
def load_margin(path: Path) -> pd.DataFrame:
    """Daily Novadata margin export, trimmed to what the OOS model needs."""
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
def load_ledger(path: Path | None):
    """Amazon FBA Inventory Ledger → daily stock panel per SKU and region.

    Returns one row per (SKU, region, Date) — sellable balance, units shipped
    (demand) and units in transit. Region is **EU** (the Pan-EU pool) or **GB**
    (the separate post-Brexit warehouse), kept apart so each is its own pool.
    Empty if no ledger file is present.
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
    led = led.rename(columns={"MSKU": "SKU"})
    for c in ("Ending Warehouse Balance", "Customer Shipments",
              "In Transit Between Warehouses", "Receipts"):
        led[c] = pd.to_numeric(led.get(c), errors="coerce").fillna(0.0)
    led = led[led["Disposition"] == "SELLABLE"].copy()
    # GB is a separate (post-Brexit) warehouse, NOT part of the Pan-EU pool.
    led["region"] = np.where(led.get("Location") == "GB", "GB", "EU")

    eu = led.groupby(["SKU", "region", "Date"], as_index=False).agg(
        eu_stock=("Ending Warehouse Balance", "sum"),
        shipped=("Customer Shipments", "sum"),
        in_transit=("In Transit Between Warehouses", "sum"),
        receipts=("Receipts", "sum"),   # genuine inbound (NOT customer returns)
    )
    eu["shipped"] = (-eu["shipped"]).clip(lower=0)  # outbound is negative
    eu["SKU"] = eu["SKU"].astype(str)
    return eu


@st.cache_data(show_spinner="Computing stock-out history…")
def compute_oos_long(margin_path: Path, ledger_path: Path | None):
    """Build the per-day OOS panel at SKU level, split into two regions.

    Demand and stock are pooled within each region — **EU** (the Pan-EU pool, all
    marketplaces except amazon.co.uk) and **GB** (the separate post-Brexit
    warehouse, amazon.co.uk). Within a region the demand rate (lambda), price,
    CM3 and ad-spend baselines are computed on the pooled totals per SKU. One
    row per (Period, SKU, region).

    Returns (long, meta, asof, eu).
    """
    df = load_margin(margin_path)
    # Region split: GB (amazon.co.uk) is its own pool, everything else is EU.
    df["region"] = np.where(
        df["Marketplace Name"].astype(str) == "amazon.co.uk", "GB", "EU")
    eu = load_ledger(ledger_path)
    keys = ["SKU", "region"]

    g = df.groupby(["Period"] + keys, observed=True, as_index=False).agg(
        Units=("Units", "sum"), Sales=("Sales", "sum"),
        CM3=("CM3", "sum"), FBA=("FBA Available", "max"), PPC=("AdSpend", "sum"),
    )
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
    roll_units = units.rolling(BASELINE_WINDOW, min_periods=1).sum()
    roll_days = units.rolling(BASELINE_WINDOW, min_periods=1).count()
    roll_sales = sales.rolling(BASELINE_WINDOW, min_periods=1).sum()
    roll_cm3 = cm3.rolling(BASELINE_WINDOW, min_periods=1).sum()
    # Ad-spend baseline = trailing avg over days that actually had spend; NaN
    # before Novadata began reporting Advertising Costs (so no false ad-cuts).
    roll_ppc = ppc.rolling(BASELINE_WINDOW, min_periods=1).sum()
    roll_ppc_days = (ppc > 0).rolling(BASELINE_WINDOW, min_periods=1).sum()
    base_ppc = (roll_ppc / roll_ppc_days.where(roll_ppc_days > 0)).ffill()
    price = sales / units.where(units > 0)  # realised price/unit per day

    # expected = average units per CALENDAR day = the demand rate. ffill keeps
    # the pre-stock-out rate alive through a zero-run, so a multi-week stock-out
    # is still measured. A zero-sales day only flags a stock-out when this rate
    # clears DEFAULT_MIN_DEMAND (so a thin marketplace's normal no-sale days are
    # not mistaken for stock-outs).
    expected = (roll_units / roll_days).ffill()
    avg_price = (roll_sales / roll_units.where(roll_units > 0)).ffill()
    avg_cm3_pu = (roll_cm3 / roll_units.where(roll_units > 0)).ffill()

    had_past = pos.cummax()
    has_future = pos[::-1].cummax()[::-1]

    def melt(frame: pd.DataFrame, name: str) -> pd.Series:
        s = frame.stack(["SKU", "region"], future_stack=True)
        s.name = name
        return s

    long = pd.concat(
        [melt(units, "units"), melt(sales, "sales"), melt(cm3, "cm3"),
         melt(fba, "fba"), melt(expected, "expected"),
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

    for col in ("units", "sales", "cm3", "fba", "expected", "avg_price",
                "avg_cm3_pu", "ppc", "base_ppc", "price", "eu_stock", "dos",
                "receipts"):
        long[col] = long[col].astype("float32")
    for col in ("SKU", "region"):
        long[col] = long[col].astype("category")

    info = df[["SKU", "Product", "Brand"]].dropna(subset=["SKU"]).copy()
    info["SKU"] = info["SKU"].astype(str)
    meta = info.drop_duplicates(subset=["SKU"], keep="last")
    long = long.sort_values(["SKU", "region", "Period"]).reset_index(drop=True)
    return long, meta, asof, eu


def flag_oos(long: pd.DataFrame, min_demand: float,
             dos_th: float = COOLDOWN_DOS, ppc_cut: float = COOLDOWN_PPC_CUT,
             price_up: float = COOLDOWN_PRICE_UP,
             min_ppc: float = COOLDOWN_MIN_PPC,
             oos_dos: float = OOS_DOS) -> pd.DataFrame:
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
    cooldown = (
        (ad_cut | hike) & cool_stock & (units > 0) & (expected >= min_demand)
        & had_past & ~phys_eu
    )

    oos_fba = fba_known & (fba == 0) & had_past
    oos_gap = (units == 0) & (expected >= min_demand) & had_past & (has_future | recent)
    # Involuntary OOS excludes days we chose to throttle (those are cooling-down).
    raw_oos = (phys_eu | low_reach | oos_fba | oos_gap) & ~cooldown

    res = long.copy()
    res["raw_oos"] = raw_oos

    # --- Keep an OOS episode open through return-driven blips ---
    # Customer returns trickle back into the warehouse and can nudge the sellable
    # balance / reach up mid-stock-out, spuriously breaking the OOS run (or even
    # tripping a cooling-down flag). A real recovery is a genuine inbound
    # *Receipt*, not a return — so once a SKU is OOS it stays OOS until the next
    # Receipts restock, as long as stock is still depleted.
    receipts = long["receipts"].to_numpy()
    restock = ~np.isnan(receipts) & (receipts >= np.maximum(10.0, expected))
    res["_restock"] = restock
    grp = [res["SKU"].values, res["region"].values]
    last_oos = res["Period"].where(res["raw_oos"]).groupby(grp).ffill()
    last_rs = res["Period"].where(res["_restock"]).groupby(grp).ffill()
    oos_open = (last_oos.notna() & (last_rs.isna() | (last_oos > last_rs))).to_numpy()
    # Only bridge days where sales are still suppressed (the OOS symptom) — so a
    # genuine recovery (sales back near λ) ends the episode even before a Receipt.
    filled = oos_open & ~np.isnan(dos) & (units < 0.5 * expected) & had_past
    oos = raw_oos | filled
    cooldown = cooldown & ~oos          # a throttle inside an OOS episode is OOS

    cause = np.where(
        phys_eu, "Physical (network)",
        np.where(oos & ~np.isnan(dos) & (dos < oos_dos), "Critically low (<%gd reach)" % oos_dos,
                 np.where(cooldown, "Cooling down",
                          np.where(oos, "Demand gap (EU)", ""))),
    )

    lost_units = np.where(oos, np.clip(expected - units, 0, None), 0.0)
    miss_units = np.where(cooldown, np.clip(expected - units, 0, None), 0.0)
    res["oos"] = oos
    res["cooldown"] = cooldown
    res["cause"] = cause
    res["lost_units"] = lost_units.astype("float32")
    res["lost_rev"] = (lost_units * np.nan_to_num(ap)).astype("float32")
    res["lost_cm3"] = (lost_units * np.nan_to_num(cm3pu)).astype("float32")
    res["miss_units"] = miss_units.astype("float32")
    res["rev_miss"] = (miss_units * np.nan_to_num(ap)).astype("float32")
    res["cm3_miss"] = (miss_units * np.nan_to_num(cm3pu)).astype("float32")
    return res.drop(columns=["raw_oos", "_restock"])


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
    return out


def sku_timeline_fig(d: pd.DataFrame, title: str) -> go.Figure:
    """Methodology-style timeline for one SKU: units/day + expected demand (λ)
    + stock (ledger), with OOS days shaded red and cooling-down days purple."""
    d = d.sort_values("Period")
    fig = go.Figure()
    fig.add_bar(x=d["Period"], y=d["units"], name="Units sold/day",
                marker_color="#2a9d8f")
    fig.add_trace(go.Scatter(x=d["Period"], y=d["expected"], name="Expected demand (λ)",
                             mode="lines", line=dict(color="#264653", dash="dot")))
    if d["eu_stock"].notna().any():
        fig.add_trace(go.Scatter(x=d["Period"], y=d["eu_stock"], name="Stock (ledger)",
                                 yaxis="y2", mode="lines", line=dict(color="#8d99ae")))
    half = pd.Timedelta(hours=12)
    for dd in d.loc[d["oos"], "Period"]:
        fig.add_vrect(x0=dd - half, x1=dd + half, fillcolor="#d32f2f",
                      opacity=0.13, line_width=0, layer="below")
    for dd in d.loc[d["cooldown"], "Period"]:
        fig.add_vrect(x0=dd - half, x1=dd + half, fillcolor="#9b5de5",
                      opacity=0.13, line_width=0, layer="below")
    fig.update_layout(
        height=360, title=title, bargap=0,
        yaxis=dict(title="Units / day"),
        yaxis2=dict(title="Stock", overlaying="y", side="right", showgrid=False),
        margin=dict(l=10, r=10, t=50, b=60),
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0.5, xanchor="center"))
    return fig
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
if margin_path is None:
    st.error(f"No margin export in `{EXPORTS_DIR}`. Run the Novadata export first.")
    st.stop()

long_all, meta, asof, eu = compute_oos_long(margin_path, ledger_path)
prod_map = meta.set_index("SKU")["Product"].astype("object")
en_titles = load_english_titles(REPO_ROOT / "product_titles_en.csv")
if not en_titles.empty:
    prod_map.update(en_titles)             # prefer English where we have it
prod_short = prod_map.map(short_product)   # readable names for tables
brand_map = meta.set_index("SKU")["Brand"]
inv = inventory_status(eu, asof)

if ledger_path is None:
    st.warning(
        "No Amazon FBA Inventory Ledger found in `amazon_ledger/` — running on "
        "Novadata signals only. Upload a ledger export to enable the physical "
        "stock-out, days-of-supply and risk views. See README_OOS.md."
    )

# ---------- Filter bar ----------
REGION_LABEL = {"EU": "🇪🇺 EU (Pan-EU)", "GB": "🇬🇧 GB (UK warehouse)"}
regions = [r for r in ["EU", "GB"] if r in set(long_all["region"].unique())]
c1, c2, c3, c4, c5 = st.columns([1.1, 0.95, 1.2, 0.9, 1.25])
region = c1.selectbox("Region", regions, index=0,
                      format_func=lambda r: REGION_LABEL.get(r, r),
                      help="EU = the Pan-EU pool (all marketplaces except "
                           "amazon.co.uk). GB = the separate UK warehouse "
                           "(amazon.co.uk). They are tracked as independent pools.")
pmin, pmax = long_all["Period"].min(), asof
ptype = c2.selectbox("Period", ["Full range", "Quarter", "Month"], index=0)
if ptype == "Month":
    opts = list(pd.period_range(pmin, pmax, freq="M"))[::-1]
    sel = c3.selectbox("Month", opts, index=0, format_func=lambda p: p.strftime("%b %Y"))
    start, end = sel.start_time, sel.end_time
elif ptype == "Quarter":
    opts = list(pd.period_range(pmin, pmax, freq="Q"))[::-1]
    sel = c3.selectbox("Quarter", opts, index=0,
                       format_func=lambda p: f"{p.year} Q{p.quarter}")
    start, end = sel.start_time, sel.end_time
else:
    c3.selectbox("Bucket", ["— full range —"], disabled=True)
    start, end = pmin, pmax
start, end = max(start, pmin), min(end, pmax)
min_demand = c4.slider(
    "Min demand (units/day)", 1.0, 10.0, DEFAULT_MIN_DEMAND, 0.5,
    help="A zero-sales day is only treated as a demand-gap stock-out when the "
         "SKU's pooled expected demand rate clears this — high enough that "
         "selling nothing is a real anomaly. Physical (ledger) stock-outs and "
         "critically-low reach always count.",
)
search = c5.text_input("SKU or Product contains", "")

# Region-scoped current-stock lookups for the ranking / status columns.
inv_r = inv[inv["region"] == region] if not inv.empty else inv
inv_stock = inv_r.set_index("SKU")["cur_stock"] if not inv_r.empty else pd.Series(dtype=float)
inv_dos = inv_r.set_index("SKU")["days_of_supply"] if not inv_r.empty else pd.Series(dtype=float)

with st.expander("Stock-out & cooling-down thresholds"):
    cc1, cc2, cc3, cc4 = st.columns(4)
    oos_reach = cc1.slider("OOS when reach below (days)", 1, 14, OOS_DOS, 1,
                           help="Reach (days-of-supply) below this counts as out "
                                "of stock, even if the balance isn't literally 0.")
    cd_dos = cc2.slider("Cool-down when reach below (days)", 5, 60, COOLDOWN_DOS, 5)
    cd_price = cc3.slider("Price hike vs baseline (%)", 2, 30,
                          int(COOLDOWN_PRICE_UP * 100), 1) / 100
    cd_ppc = cc4.slider("Ad-spend cut vs baseline (%)", 20, 90,
                        int(COOLDOWN_PPC_CUT * 100), 5) / 100
    st.caption("OOS = reach below the first threshold (or balance 0 / a demand "
               "gap). 'Cooling down' = the SKU throttled (price up, and/or ad "
               "spend cut) while reach is between the OOS and cool-down "
               "thresholds. Ad-cut detection only applies from when Novadata "
               "began reporting Advertising Costs (~Feb 2026); the price lever "
               "works across the full year.")

# ---------- Apply scope ----------
scope = flag_oos(long_all, min_demand, dos_th=cd_dos, ppc_cut=cd_ppc,
                 price_up=cd_price, oos_dos=oos_reach)
scope = scope[(scope["region"] == region)
              & (scope["Period"] >= start) & (scope["Period"] <= end)]
if search.strip():
    s = search.strip().lower()
    skus = scope["SKU"].astype(str).str.lower()
    prods = scope["SKU"].astype(str).map(prod_map).astype(str).str.lower()
    scope = scope[skus.str.contains(s) | prods.str.contains(s, na=False)]
if scope.empty:
    st.warning("No data in the current scope. Widen the filters.")
    st.stop()

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
agg["Status"] = np.where(
    agg["cur_stock"].fillna(-1) == 0, "🔴 Out of stock",
    np.where(agg["days_of_supply"].fillna(1e9) < LOW_STOCK_DAYS, "🟠 Low stock",
             np.where(agg["cur_stock"].isna(), "❔ Unknown", "🟢 In stock")),
)
agg = agg.sort_values("lost_cm3", ascending=False)

# ---------- KPI tiles (split: involuntary OOS vs voluntary cooling-down) ----------
cd_rows = scope[scope["cooldown"]]

st.markdown("**🔴 Out of stock — involuntary (lost)**")
o1, o2, o3, o4 = st.columns(4)
o1.metric("SKUs affected", fmt_num(agg["SKU"].nunique()))
o2.metric("Lost revenue", eur(agg["lost_rev"].sum()))
o3.metric("Lost CM3 (P&L impact)", eur(agg["lost_cm3"].sum()))
o4.metric("OOS SKU-days", fmt_num(len(oos_rows)))

st.markdown("**🟣 Cooling down — voluntary throttle (miss)**")
c1, c2, c3, c4 = st.columns(4)
c1.metric("SKUs cooled down", fmt_num(cd_rows["SKU"].nunique()))
c2.metric("Miss revenue", eur(cd_rows["rev_miss"].sum()))
c3.metric("Miss CM3", eur(cd_rows["cm3_miss"].sum()))
c4.metric("Cool-down SKU-days", fmt_num(len(cd_rows)))

st.caption(
    f"Data through **{asof.date()}** · scope **{start.date()} → {end.date()}** "
    f"· region **{REGION_LABEL.get(region, region)}**. *Lost* = sales forfeited while out of stock "
    "(involuntary); *Miss* = sales given up by deliberately throttling demand "
    "(price up / ad cut) to avoid OOS. Both valued as contribution margin (CM3) "
    "— the true P&L impact."
)

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Most affected SKUs", "Impact over time", "Inventory & risk",
     "Stock-out events", "Cooling down"]
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
    fig.update_layout(height=max(320, 26 * top_n), margin=dict(l=10, r=10, t=50, b=10))
    fig.update_traces(marker_color="#d32f2f")
    st.plotly_chart(fig, width="stretch")

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
    freq_label = st.radio("Bucket", ["Month", "Quarter"], horizontal=True)
    per = "M" if freq_label == "Month" else "Q"
    ts = oos_rows.copy()
    ts["bucket"] = ts["Period"].dt.to_period(per).dt.to_timestamp()
    by_bucket = ts.groupby("bucket", observed=True).agg(
        lost_rev=("lost_rev", "sum"), lost_cm3=("lost_cm3", "sum"),
        oos_days=("Period", "count")).reset_index()

    fig2 = go.Figure()
    fig2.add_bar(x=by_bucket["bucket"], y=by_bucket["lost_rev"],
                 name="Lost revenue (€)", marker_color="#f4a261")
    fig2.add_bar(x=by_bucket["bucket"], y=by_bucket["lost_cm3"],
                 name="Lost CM3 (€)", marker_color="#d32f2f")
    fig2.add_trace(go.Scatter(x=by_bucket["bucket"], y=by_bucket["oos_days"],
                              name="OOS SKU-days", yaxis="y2",
                              mode="lines+markers", line=dict(color="#264653")))
    fig2.update_layout(
        barmode="group", height=380, title=f"Lost impact by {freq_label.lower()}",
        yaxis=dict(title="€ lost"),
        yaxis2=dict(title="OOS SKU-days", overlaying="y", side="right", showgrid=False),
        margin=dict(l=10, r=10, t=50, b=60),
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0.5, xanchor="center"))
    st.plotly_chart(fig2, width="stretch")

    st.subheader("Stock-out calendar — top SKUs")
    n_heat = st.slider("SKUs in heatmap", 5, 40, 15, key="heat")
    top_skus = agg.head(n_heat)["SKU"].tolist()
    hm = ts[ts["SKU"].isin(top_skus)]
    if not hm.empty:
        pv = hm.pivot_table(index="SKU", columns="bucket", values="Period",
                            aggfunc="count", observed=True).reindex(top_skus)
        pv.columns = [c.strftime("%b %y") for c in pv.columns]
        figh = px.imshow(pv, color_continuous_scale="Reds", aspect="auto",
                         labels=dict(color="OOS days"))
        figh.update_layout(height=max(300, 24 * n_heat), margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(figh, width="stretch")

    st.subheader("SKU drill-down")
    sel_sku = st.selectbox("SKU", agg["SKU"].tolist(), key="drill")
    drill = scope[scope["SKU"] == sel_sku].sort_values("Period")
    d = drill.groupby("Period", observed=True).agg(
        units=("units", "sum"), expected=("expected", "mean"),
        eu_stock=("eu_stock", "max"), oos=("oos", "max")).reset_index()
    figd = go.Figure()
    figd.add_bar(x=d["Period"], y=d["units"], name="Units sold", marker_color="#2a9d8f")
    figd.add_trace(go.Scatter(x=d["Period"], y=d["expected"], name="Expected demand",
                              mode="lines", line=dict(color="#264653", dash="dot")))
    if d["eu_stock"].notna().any():
        figd.add_trace(go.Scatter(x=d["Period"], y=d["eu_stock"], name="EU stock (ledger)",
                                  yaxis="y2", mode="lines", line=dict(color="#8d99ae")))
    for dd in d[d["oos"]]["Period"]:
        figd.add_vrect(x0=dd - pd.Timedelta(hours=12), x1=dd + pd.Timedelta(hours=12),
                       fillcolor="#d32f2f", opacity=0.12, line_width=0)
    figd.update_layout(
        height=380, title=f"{sel_sku} — sales, demand & stock (OOS days shaded)",
        yaxis=dict(title="Units / day"),
        yaxis2=dict(title="EU stock", overlaying="y", side="right", showgrid=False),
        margin=dict(l=10, r=10, t=50, b=60),
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0.5, xanchor="center"))
    st.plotly_chart(figd, width="stretch")

# ======================================================================
#  Tab 3 — Inventory & risk (ledger-driven)
# ======================================================================
with tab3:
    if inv_r.empty:
        st.info("Upload an Amazon FBA Inventory Ledger to `amazon_ledger/` to "
                "enable days-of-supply and low-stock risk. See README_OOS.md.")
    else:
        risk = inv_r.copy()
        risk["Product"] = risk["SKU"].map(prod_short)
        if search.strip():
            s = search.strip().lower()
            risk = risk[risk["SKU"].str.lower().str.contains(s)
                        | risk["Product"].astype(str).str.lower().str.contains(s, na=False)]
        low = risk[(risk["days_of_supply"] < LOW_STOCK_DAYS)
                   | (risk["cur_stock"] <= 0)].sort_values("days_of_supply")
        r1, r2, r3 = st.columns(3)
        r1.metric("SKUs currently out of stock", fmt_num(int((risk["cur_stock"] <= 0).sum())))
        r2.metric(f"Low stock (< {LOW_STOCK_DAYS}d supply)",
                  fmt_num(int((risk["days_of_supply"] < LOW_STOCK_DAYS).sum())))
        r3.metric("Units in transit (sel.)", fmt_num(risk["in_transit"].sum()))
        st.subheader(f"Stock-out risk — replenish first (as of {asof.date()})")
        disp = low[["SKU", "Product", "cur_stock", "in_transit",
                    "avg_daily_demand", "days_of_supply"]].rename(columns={
            "cur_stock": "Current stock", "in_transit": "In transit",
            "avg_daily_demand": "Avg demand/day", "days_of_supply": "Days of supply"})
        disp = disp.round(0)
        st.dataframe(
            disp, width="stretch", hide_index=True, height=460,
            column_config={
                "Current stock": st.column_config.NumberColumn(format="localized"),
                "In transit": st.column_config.NumberColumn(format="localized"),
                "Avg demand/day": st.column_config.NumberColumn(format="localized"),
                "Days of supply": st.column_config.NumberColumn(format="localized")})
        st.caption("Days of supply = current EU sellable stock ÷ trailing 30-day "
                   "average units shipped. Sorted lowest-first.")

# ======================================================================
#  Tab 4 — Stock-out events
# ======================================================================
with tab4:
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
#  Tab 5 — Cooling down (deliberate demand throttling to avoid OOS)
# ======================================================================
with tab5:
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
