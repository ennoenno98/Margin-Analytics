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
     Per-SKU per-marketplace daily Units / Sales / CM3. Gives marketplace-level
     lost sales (a SKU that normally sells in DE suddenly goes quiet) and the
     price + contribution margin per unit needed to value lost units in €.

A SKU×marketplace is OUT OF STOCK on a day when EITHER:
  * the EU network sellable balance is 0 (real physical stock-out — ledger), OR
  * FBA Available is 0 (Novadata snapshot), OR
  * the marketplace went quiet: Units == 0 on a day enclosed by sales, for a
    SKU whose demand rate is high enough that selling nothing is a real anomaly.
Each OOS day is tagged with its cause: "Physical (network)" when the EU pool is
empty, otherwise "Marketplace gap" (sales stopped despite EU stock — offer
suppression, buy-box loss, listing issue, …).

Lost units = expected daily demand − whatever still sold; valued at the SKU's
trailing avg price (→ lost revenue) and avg CM3 per unit (→ lost CM3, the P&L
impact).

Separately, "Cooling down" days are when demand was *deliberately throttled*
(ad spend cut and/or price raised) while stock was tight, to avoid a hard
stock-out. Those are tagged distinctly and their forgone sales booked as
revenue/CM3 *miss* (voluntary) rather than *lost* (involuntary), so the two are
never double-counted. Category priority: Physical (network) > Cooling down >
Marketplace gap.

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
    """Amazon FBA Inventory Ledger → EU-pooled daily panel.

    Returns one row per (SKU, Date) — sellable EU network balance, units
    shipped to customers (demand) and units in transit. The seller runs Pan-EU,
    so availability is pooled across the network: a SKU is physically out of
    stock only when the *whole* EU sellable balance hits zero. Empty if no
    ledger file is present (the tool then degrades to Novadata signals only).
    """
    empty_eu = pd.DataFrame(columns=["SKU", "Date", "eu_stock", "shipped", "in_transit"])
    if path is None:
        return empty_eu
    KEEP = {
        "Date", "MSKU", "Location", "Disposition",
        "Ending Warehouse Balance", "Customer Shipments",
        "In Transit Between Warehouses",
    }
    led = pd.read_csv(path, usecols=lambda c: c in KEEP)
    led["Date"] = pd.to_datetime(led["Date"], format="%m/%d/%Y", errors="coerce")
    led = led.rename(columns={"MSKU": "SKU"})
    for c in ("Ending Warehouse Balance", "Customer Shipments",
              "In Transit Between Warehouses"):
        led[c] = pd.to_numeric(led.get(c), errors="coerce").fillna(0.0)
    led = led[led["Disposition"] == "SELLABLE"]

    eu = led.groupby(["SKU", "Date"], as_index=False).agg(
        eu_stock=("Ending Warehouse Balance", "sum"),
        shipped=("Customer Shipments", "sum"),
        in_transit=("In Transit Between Warehouses", "sum"),
    )
    eu["shipped"] = (-eu["shipped"]).clip(lower=0)  # outbound is negative
    eu["SKU"] = eu["SKU"].astype(str)
    return eu


@st.cache_data(show_spinner="Computing stock-out history…")
def compute_oos_long(margin_path: Path, ledger_path: Path | None):
    """Build the per-day OOS panel from both sources.

    Returns (long, meta, asof, eu):
      long : one row per (Period, SKU, Marketplace Name) for every active
             product-day, with the demand baseline, raw signals and the merged
             ledger stock columns. flag_oos() turns this into OOS flags cheaply.
      meta : per-SKU lookup (Product, Brand).
      asof : most recent date.
      eu   : EU-pooled ledger panel (for the inventory & risk view).
    """
    df = load_margin(margin_path)
    eu = load_ledger(ledger_path)
    keys = ["SKU", "Marketplace Name"]

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
        s = frame.stack(["SKU", "Marketplace Name"], future_stack=True)
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

    # --- Merge the EU-pooled Amazon ledger stock signal ---
    long["SKU"] = long["SKU"].astype(str)
    long["Marketplace Name"] = long["Marketplace Name"].astype(str)
    if not eu.empty:
        # Bring EU stock and a SKU-level days-of-supply (stock / trailing demand).
        e = eu.sort_values("Date").copy()
        e["avgship"] = e.groupby("SKU")["shipped"].transform(
            lambda s: s.rolling(28, min_periods=5).mean())
        e["dos"] = e["eu_stock"] / e["avgship"].where(e["avgship"] > 0)
        long = long.merge(e[["SKU", "Date", "eu_stock", "dos"]],
                          left_on=["SKU", "Period"], right_on=["SKU", "Date"],
                          how="left").drop(columns=["Date"])
    else:
        long["eu_stock"] = np.nan
        long["dos"] = np.nan

    for col in ("units", "sales", "cm3", "fba", "expected", "avg_price",
                "avg_cm3_pu", "ppc", "base_ppc", "price", "eu_stock", "dos"):
        long[col] = long[col].astype("float32")
    for col in ("SKU", "Marketplace Name"):
        long[col] = long[col].astype("category")

    info = df[["SKU", "Product", "Brand"]].dropna(subset=["SKU"]).copy()
    info["SKU"] = info["SKU"].astype(str)
    meta = info.drop_duplicates(subset=["SKU"], keep="last")
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
    # (reach below the cool-down threshold but not yet into the OOS zone).
    ad_cut = ~np.isnan(base_ppc) & (base_ppc > min_ppc) & (ppc <= base_ppc * (1 - ppc_cut))
    hike = ~np.isnan(price) & (price >= ap * (1 + price_up))
    cool_stock = ~np.isnan(dos) & (dos < dos_th) & (dos >= oos_dos)
    cooldown = (
        (ad_cut | hike) & cool_stock & (expected >= min_demand) & had_past
        & ~phys_eu
    )

    oos_fba = fba_known & (fba == 0) & had_past
    oos_gap = (units == 0) & (expected >= min_demand) & had_past & (has_future | recent)
    # Involuntary OOS excludes days we chose to throttle (those are cooling-down).
    oos = (phys_eu | low_reach | oos_fba | oos_gap) & ~cooldown

    cause = np.where(
        phys_eu, "Physical (network)",
        np.where(low_reach, "Critically low (<%gd reach)" % oos_dos,
                 np.where(cooldown, "Cooling down",
                          np.where(oos, "Marketplace gap", ""))),
    )

    lost_units = np.where(oos, np.clip(expected - units, 0, None), 0.0)
    miss_units = np.where(cooldown, np.clip(expected - units, 0, None), 0.0)
    res = long.copy()
    res["oos"] = oos
    res["cooldown"] = cooldown
    res["cause"] = cause
    res["lost_units"] = lost_units.astype("float32")
    res["lost_rev"] = (lost_units * np.nan_to_num(ap)).astype("float32")
    res["lost_cm3"] = (lost_units * np.nan_to_num(cm3pu)).astype("float32")
    res["miss_units"] = miss_units.astype("float32")
    res["rev_miss"] = (miss_units * np.nan_to_num(ap)).astype("float32")
    res["cm3_miss"] = (miss_units * np.nan_to_num(cm3pu)).astype("float32")
    return res


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


def stockout_events(scope: pd.DataFrame) -> pd.DataFrame:
    """Collapse consecutive OOS days into events per SKU×marketplace."""
    d = scope[scope["oos"]].copy()
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values(["SKU", "Marketplace Name", "Period"])
    keys = ["SKU", "Marketplace Name"]
    gap = d.groupby(keys, observed=True)["Period"].diff().dt.days.fillna(1)
    d["event_id"] = (gap > 1).groupby(
        [d["SKU"].values, d["Marketplace Name"].values]
    ).cumsum().values
    ev = (
        d.groupby(keys + ["event_id"], observed=True)
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
    """Current stock + days-of-supply per SKU from the EU-pooled ledger."""
    if eu.empty:
        return pd.DataFrame()
    eu = eu.sort_values("Date")
    last = eu.groupby("SKU").agg(cur_stock=("eu_stock", "last"),
                                 in_transit=("in_transit", "last"),
                                 last_date=("Date", "max")).reset_index()
    recent = eu[eu["Date"] >= (eu["Date"].max() - pd.Timedelta(days=30))]
    demand = recent.groupby("SKU")["shipped"].mean().rename("avg_daily_demand")
    out = last.merge(demand, on="SKU", how="left")
    out["days_of_supply"] = (
        out["cur_stock"] / out["avg_daily_demand"].where(out["avg_daily_demand"] > 0)
    )
    return out


# ======================================================================
#  App
# ======================================================================
require_login()

st.title("📦 OOS Impact Analytics")
st.caption(
    "Tracks Amazon out-of-stock impact over the year — estimated lost revenue "
    "& contribution margin per SKU. Hybrid of the FBA Inventory Ledger (real "
    "stock) and the Novadata margin export (marketplace sales & €)."
)

margin_path = latest_export(EXPORTS_DIR)
ledger_path = latest_ledger(LEDGER_DIR)
if margin_path is None:
    st.error(f"No margin export in `{EXPORTS_DIR}`. Run the Novadata export first.")
    st.stop()

long_all, meta, asof, eu = compute_oos_long(margin_path, ledger_path)
prod_map = meta.set_index("SKU")["Product"]
prod_short = prod_map.map(short_product)   # readable names for tables
brand_map = meta.set_index("SKU")["Brand"]
inv = inventory_status(eu, asof)
inv_stock = inv.set_index("SKU")["cur_stock"] if not inv.empty else pd.Series(dtype=float)
inv_dos = inv.set_index("SKU")["days_of_supply"] if not inv.empty else pd.Series(dtype=float)

if ledger_path is None:
    st.warning(
        "No Amazon FBA Inventory Ledger found in `amazon_ledger/` — running on "
        "Novadata signals only. Upload a ledger export to enable the physical "
        "stock-out, days-of-supply and risk views. See README_OOS.md."
    )

# ---------- Filter bar ----------
markets = sorted(long_all["Marketplace Name"].dropna().unique().tolist())
c1, c2, c3, c4 = st.columns([1.2, 1.6, 1, 1.4])
market = c1.selectbox("Marketplace", ["🌍 All countries"] + markets, index=0)
min_date, max_date = long_all["Period"].min().date(), asof.date()
date_range = c2.date_input("Date range", value=(min_date, max_date),
                           min_value=min_date, max_value=max_date)
min_demand = c3.slider(
    "Min demand (units/day)", 1.0, 10.0, DEFAULT_MIN_DEMAND, 0.5,
    help="A zero-sales day is only treated as a marketplace stock-out when the "
         "SKU's expected demand rate clears this — high enough that selling "
         "nothing is a real anomaly. Physical (ledger) stock-outs always count.",
)
search = c4.text_input("SKU or Product contains", "")

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

if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start, end = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
else:
    start, end = long_all["Period"].min(), asof

# ---------- Apply scope ----------
scope = flag_oos(long_all, min_demand, dos_th=cd_dos, ppc_cut=cd_ppc,
                 price_up=cd_price, oos_dos=oos_reach)
scope = scope[(scope["Period"] >= start) & (scope["Period"] <= end)]
if market != "🌍 All countries":
    scope = scope[scope["Marketplace Name"] == market]
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
agg["Product"] = agg["SKU"].map(prod_map)
agg["Brand"] = agg["SKU"].map(brand_map)
agg["cur_stock"] = agg["SKU"].map(inv_stock)
agg["days_of_supply"] = agg["SKU"].map(inv_dos).round(0)
agg["Status"] = np.where(
    agg["cur_stock"].fillna(-1) == 0, "🔴 Out of stock",
    np.where(agg["days_of_supply"].fillna(1e9) < LOW_STOCK_DAYS, "🟠 Low stock",
             np.where(agg["cur_stock"].isna(), "❔ Unknown", "🟢 In stock")),
)
agg = agg.sort_values("lost_cm3", ascending=False)

# ---------- KPI tiles ----------
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("SKUs affected", fmt_num(agg["SKU"].nunique()))
k2.metric("Lost revenue", eur(agg["lost_rev"].sum()))
k3.metric("Lost CM3 (P&L impact)", eur(agg["lost_cm3"].sum()))
k4.metric("Lost units", fmt_num(agg["lost_units"].sum()))
phys_days = int((oos_rows["cause"] == "Physical (network)").sum())
k5.metric("Physical stock-out days", fmt_num(phys_days))
st.caption(
    f"Data through **{asof.date()}** · scope **{start.date()} → {end.date()}** "
    f"· marketplace **{market}**. Lost CM3 is the estimated contribution margin "
    "(€) forfeited while out of stock — the true P&L impact."
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
        margin=dict(l=10, r=10, t=50, b=10), legend=dict(orientation="h", y=1.12))
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
    if market == "🌍 All countries" and drill["Marketplace Name"].nunique() > 1:
        mkts = sorted(drill["Marketplace Name"].dropna().unique().tolist())
        sel_mkt = st.selectbox("Marketplace", mkts, key="drillmkt")
        drill = drill[drill["Marketplace Name"] == sel_mkt]
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
        margin=dict(l=10, r=10, t=50, b=10), legend=dict(orientation="h", y=1.12))
    st.plotly_chart(figd, width="stretch")

# ======================================================================
#  Tab 3 — Inventory & risk (ledger-driven)
# ======================================================================
with tab3:
    if inv.empty:
        st.info("Upload an Amazon FBA Inventory Ledger to `amazon_ledger/` to "
                "enable days-of-supply and low-stock risk. See README_OOS.md.")
    else:
        risk = inv.copy()
        risk["Product"] = risk["SKU"].map(prod_map)
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
        ev["Product"] = ev["SKU"].map(prod_map)
        ev = ev.sort_values("lost_cm3", ascending=False)
        disp = ev[["SKU", "Product", "Marketplace Name", "cause", "Start", "End",
                   "Days", "lost_units", "lost_rev", "lost_cm3"]].rename(columns={
            "Marketplace Name": "Marketplace", "cause": "Cause",
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
        cagg["Product"] = cagg["SKU"].map(prod_map)
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
