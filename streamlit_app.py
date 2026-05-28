"""Margin Analytics dashboard (Streamlit).

Reads the latest CSV in `novadata_exports/` (committed by the GitHub Actions
workflow `.github/workflows/weekly_export.yml`, which runs
`novadata_weekly_export.py --once`) and renders an interactive,
password-gated dashboard.

Run locally:
    pip install -r requirements.txt
    python novadata_weekly_export.py --once   # populate novadata_exports/
    export DASHBOARD_PASSWORD="choose-a-password"
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = REPO_ROOT / "novadata_exports"

st.set_page_config(
    page_title="Margin Analytics",
    page_icon="📈",
    layout="wide",
)


# ---------- Auth ----------
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

    st.title("Margin Analytics")
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


# ---------- Data ----------
def latest_export(exports_dir: Path) -> Path | None:
    if not exports_dir.exists():
        return None
    # Match .csv and .csv.gz; sort takes lexical order, which works for the
    # date-stamped filenames we use.
    candidates = sorted(
        list(exports_dir.glob("margin_export_*.csv"))
        + list(exports_dir.glob("margin_export_*.csv.gz"))
    )
    return candidates[-1] if candidates else None


@st.cache_data(show_spinner=False)
def load_data(path: Path) -> pd.DataFrame:
    # pd.read_csv handles .gz transparently when the suffix is .csv.gz.
    df = pd.read_csv(path)
    df["Period"] = pd.to_datetime(df["Period"], errors="coerce")

    # Schema variants:
    # - Daily (current): Contribution Margin 1/2/3 (€ absolute), Advertising Costs.
    # - Weekly (legacy): CM1%/CM2%/CM3%, Sponsored Spend.
    cm_abs = {
        "Contribution Margin 1": "CM1",
        "Contribution Margin 2": "CM2",
        "Contribution Margin 3": "CM3",
    }
    for src, short in cm_abs.items():
        if src in df.columns:
            df[short] = pd.to_numeric(df[src], errors="coerce")

    if "Advertising Costs" in df.columns:
        df["Sponsored Spend"] = pd.to_numeric(df["Advertising Costs"], errors="coerce")

    numeric_cols = [
        "CM1%", "CM2%", "CM3%", "Sponsored Spend", "ROAS", "CTR",
        "Orders", "Units", "Product Sales",
        "FBA Available", "Days of Supply", "Sales Velocity",
        "CM1", "CM2", "CM3",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Derive CM%s from absolute amounts when only the daily schema is present.
    sales = df.get("Product Sales")
    if sales is not None:
        for short in ["CM1", "CM2", "CM3"]:
            pct_col = f"{short}%"
            if short in df.columns and pct_col not in df.columns:
                df[pct_col] = df[short] / sales.replace(0, pd.NA) * 100

    # Derive Days of Supply if absent: stock / daily velocity.
    if "Days of Supply" not in df.columns and "FBA Available" in df.columns and "Sales Velocity" in df.columns:
        df["Days of Supply"] = df["FBA Available"] / df["Sales Velocity"].replace(0, pd.NA)

    return df


TOP_SELLER_COL = {
    "amazon.de": "DE filters",
    "amazon.co.uk": "UK filters",
    "amazon.fr": "FR filters",
    "amazon.es": "ES filters",
    "amazon.it": "IT filters",
}

CLUSTER_NAMES = {
    "1-1": "High margin · High sales",
    "1-2": "High margin · Mid sales",
    "1-3": "High margin · Low sales",
    "2-1": "Mid margin · High sales",
    "2-2": "Mid margin · Mid sales",
    "2-3": "Mid margin · Low sales",
    "3-1": "Low margin · High sales",
    "3-2": "Low margin · Mid sales",
    "3-3": "Low margin · Low sales",
}


def cluster_name(code) -> str:
    if not isinstance(code, str):
        return ""
    return CLUSTER_NAMES.get(code, code)


def tier_1_to_3(values: pd.Series) -> pd.Series:
    """Bucket a numeric series into tiers 1 (highest) → 3 (lowest).

    Ties broken by row order. Rows with NaN stay NaN.
    """
    s = pd.to_numeric(values, errors="coerce")
    if s.notna().sum() < 3:
        return pd.Series(pd.NA, index=values.index, dtype="Int64")
    ranks = s.rank(method="first", ascending=False)
    try:
        tiers = pd.qcut(ranks, q=3, labels=[1, 2, 3])
    except ValueError:
        return pd.Series(pd.NA, index=values.index, dtype="Int64")
    return tiers.astype("Int64")


COMMENTS_PATH = REPO_ROOT / "comments.json"


def load_comments() -> dict[str, str]:
    if not COMMENTS_PATH.exists():
        return {}
    try:
        return {str(k): str(v) for k, v in json.loads(COMMENTS_PATH.read_text()).items()}
    except Exception:
        return {}


def save_comments(comments: dict[str, str]) -> None:
    clean = {k: v for k, v in comments.items() if v and v.strip()}
    COMMENTS_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False))


SUM_COLS = [
    "Orders", "Units", "Product Sales", "Sponsored Spend",
    "CM1", "CM2", "CM3",  # absolute € margins from the daily export
]
WAVG_COLS = ["CM1%", "CM2%", "CM3%", "ROAS", "CTR"]
FIRST_COLS = ["Product", "Child ASIN", "Marketplace Name",
              "DE filters", "UK filters", "FR filters", "ES filters", "IT filters"]


def aggregate_periods(df_in: pd.DataFrame) -> pd.DataFrame:
    """Roll a multi-week slice up to one row per SKU.

    - Sum: Orders, Units, Product Sales, Sponsored Spend.
    - Weighted average (weight = Product Sales): margins, ROAS, CTR.
    - First non-null: descriptive columns (Product, Child ASIN, …).
    """
    if df_in.empty:
        return df_in.copy()
    sum_cols = [c for c in SUM_COLS if c in df_in.columns]
    first_cols = [c for c in FIRST_COLS if c in df_in.columns]
    wavg_cols = [c for c in WAVG_COLS if c in df_in.columns]

    base = (
        df_in.groupby("SKU", as_index=False, sort=False)
        .agg({**{c: "first" for c in first_cols}, **{c: "sum" for c in sum_cols}})
    )
    for c in wavg_cols:
        weights = pd.to_numeric(df_in["Product Sales"], errors="coerce")
        values = pd.to_numeric(df_in[c], errors="coerce")
        valid = values.notna() & weights.notna() & (weights > 0)
        tmp = pd.DataFrame({
            "SKU": df_in["SKU"],
            "_v": (values * weights).where(valid),
            "_w": weights.where(valid),
        })
        agg = tmp.groupby("SKU").agg(num=("_v", "sum"), den=("_w", "sum"))
        agg[c] = agg["num"] / agg["den"].replace(0, pd.NA)
        base = base.merge(agg[[c]], left_on="SKU", right_index=True, how="left")

    # When absolute margins are present (daily schema), prefer the cleaner
    # derivation: %CM = Σ(CM) / Σ(Sales) × 100. This overrides the weighted-avg
    # values computed above where possible.
    if "Product Sales" in base.columns:
        sales_total = pd.to_numeric(base["Product Sales"], errors="coerce")
        for short in ["CM1", "CM2", "CM3"]:
            if short in base.columns:
                cm_total = pd.to_numeric(base[short], errors="coerce")
                base[f"{short}%"] = cm_total / sales_total.replace(0, pd.NA) * 100
    return base


# ---------- App ----------
require_login()

st.title("📈 Margin Analytics")

data_path = latest_export(EXPORTS_DIR)
if data_path is None:
    st.error(
        f"No export found in `{EXPORTS_DIR}`. "
        "Run `python novadata_weekly_export.py --once` locally, or trigger "
        "the **Novadata Weekly Export** workflow in GitHub Actions."
    )
    st.stop()

df = load_data(data_path)
last_modified = pd.to_datetime(data_path.stat().st_mtime, unit="s")
st.caption(
    f"Source: `{data_path.name}` · "
    f"last refreshed **{last_modified:%Y-%m-%d %H:%M UTC}** · "
    f"{len(df):,} rows · {df['SKU'].nunique():,} SKUs"
)

# ----- Sidebar thresholds (shared across tabs) -----
target_cm3 = st.sidebar.number_input("Target CM3 %", value=19.7, step=0.5)
target_cm2 = st.sidebar.number_input("Target CM2 %", value=33.2, step=0.5)
target_cm1 = st.sidebar.number_input("Target CM1 %", value=71.0, step=0.5)
min_dos = st.sidebar.number_input("Min Days of Supply", value=30, step=5)
st.sidebar.caption("Targets affect color highlights and reference lines.")

# ----- Top filters (shared by Overview + Compare) -----
with st.container(border=True):
    c1, c2, c3, c4, c5 = st.columns([1.2, 0.9, 1.6, 1.8, 1])
    real_marketplaces = sorted(df["Marketplace Name"].dropna().unique().tolist())
    ALL_OPTION = "🌍 All countries"
    marketplaces = [ALL_OPTION] + real_marketplaces
    default_mp = "amazon.de" if "amazon.de" in real_marketplaces else real_marketplaces[0]
    marketplace = c1.selectbox("Marketplace", marketplaces, index=marketplaces.index(default_mp))
    is_all_countries = (marketplace == ALL_OPTION)

    periods = sorted(df["Period"].dropna().unique(), reverse=True)

    def _fmt_week(d):
        ts = pd.Timestamp(d)
        iso = ts.isocalendar()
        return f"KW {iso.week:02d} · {iso.year}"

    _fmt_day = lambda d: pd.Timestamp(d).strftime("%a %d %b %Y")

    # Default to Week so the multiselect isn't overwhelmed by 200+ daily options
    # when the data is daily.
    granularity_options = ["Day", "Week", "Month", "Quarter"]
    granularity = c2.radio(
        "Granularity",
        granularity_options,
        index=granularity_options.index("Week"),
        horizontal=False,
        key="granularity",
    )

    # Build bucket options for the multiselect based on granularity. Each bucket
    # maps to one or more underlying period rows; `selected_periods` stays as a
    # list of the data's native period timestamps so downstream code is unchanged.
    if granularity == "Day":
        bucket_options = list(periods)
        bucket_fmt = _fmt_day
        def _bucket_to_weeks(b):
            return [pd.Timestamp(b)]
        bucket_label = "Day(s)"
    elif granularity == "Week":
        weeks = sorted(
            {pd.Timestamp(p).to_period("W-MON").start_time for p in periods},
            reverse=True,
        )
        bucket_options = weeks
        bucket_fmt = _fmt_week
        def _bucket_to_weeks(b):
            wp = pd.Timestamp(b).to_period("W-MON")
            return [p for p in periods if pd.Timestamp(p).to_period("W-MON") == wp]
        bucket_label = "Calendar week(s)"
    elif granularity == "Month":
        months = sorted({pd.Timestamp(p).to_period("M") for p in periods}, reverse=True)
        bucket_options = months
        bucket_fmt = lambda m: m.strftime("%b %Y")
        def _bucket_to_weeks(b):
            return [p for p in periods if pd.Timestamp(p).to_period("M") == b]
        bucket_label = "Month(s)"
    else:  # Quarter
        quarters = sorted({pd.Timestamp(p).to_period("Q") for p in periods}, reverse=True)
        bucket_options = quarters
        bucket_fmt = lambda q: f"Q{q.quarter} {q.year}"
        def _bucket_to_weeks(b):
            return [p for p in periods if pd.Timestamp(p).to_period("Q") == b]
        bucket_label = "Quarter(s)"

    selected_buckets = c3.multiselect(
        bucket_label,
        bucket_options,
        default=[bucket_options[0]] if bucket_options else [],
        format_func=bucket_fmt,
        key=f"buckets_{granularity}",
        help="Pick one or several. Month/Quarter selections expand to all weeks they contain.",
    )
    if not selected_buckets and bucket_options:
        selected_buckets = [bucket_options[0]]

    expanded = []
    for b in selected_buckets:
        expanded.extend(_bucket_to_weeks(b))
    selected_periods = sorted(set(expanded), reverse=True) or [periods[0]]
    period = max(selected_periods)

    sku_query = c4.text_input("SKU or Product contains", "")
    top_only = c5.toggle("Top sellers only", value=False)

# Marketplace base slice (all weeks for this marketplace)
mp_slice = df.copy() if is_all_countries else df[df["Marketplace Name"] == marketplace].copy()

# Current view: aggregate across the selected weeks (sum + weighted avg).
raw_slice = mp_slice[mp_slice["Period"].isin(selected_periods)].copy()
# Aggregate when multiple weeks are picked, or when 'All countries' is on (the
# same SKU appears in several marketplaces and must be rolled up).
needs_aggregation = (len(selected_periods) > 1) or is_all_countries
filtered = aggregate_periods(raw_slice) if needs_aggregation else raw_slice.copy()
is_multi_period = len(selected_periods) > 1

if sku_query.strip():
    q = sku_query.strip().lower()
    filtered = filtered[
        filtered["SKU"].astype(str).str.lower().str.contains(q, na=False)
        | filtered["Product"].astype(str).str.lower().str.contains(q, na=False)
    ]

top_col = TOP_SELLER_COL.get(marketplace) if not is_all_countries else None
if top_only:
    if top_col and top_col in filtered.columns:
        filtered = filtered[filtered[top_col] == "Top Seller"]
    elif is_all_countries:
        st.info("Top-seller filter is per-marketplace; pick a single country to use it.")
    else:
        st.info(f"No top-seller flag column for {marketplace}; ignoring filter.")

# ----- KPIs -----
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("SKUs in view", f"{len(filtered):,}")
total_sales_kpi = filtered["Product Sales"].sum()
k2.metric("Total sales (€)", f"{total_sales_kpi:,.0f}")
if "CM3" in filtered.columns:
    pnl_kpi = pd.to_numeric(filtered["CM3"], errors="coerce").sum()
else:
    pnl_kpi = ((pd.to_numeric(filtered["CM3%"], errors="coerce") / 100)
               * pd.to_numeric(filtered["Product Sales"], errors="coerce")).sum()
k3.metric("P&L Impact (€)", f"{pnl_kpi:,.0f}", help="Σ Contribution Margin 3 — absolute € contribution in the current view.")
avg_cm3 = filtered["CM3%"].mean()
k4.metric("Avg CM3 %", f"{avg_cm3:,.1f}" if pd.notna(avg_cm3) else "—")
below = int((filtered["CM3%"] < target_cm3).sum())
k5.metric(f"SKUs below {target_cm3:.0f}% CM3", f"{below:,}")

tab_overview, tab_compare = st.tabs(["Overview", "Compare"])

# =========================================================================
# Overview tab — table + Δ vs previous period
# =========================================================================
with tab_overview:
    # ----- Δ CM3% vs the equivalent prior set (shift the selection back 1 week) -----
    # In single-week mode this is just the previous week; in multi-week mode we
    # shift every selected week back by 1 week, aggregate, and compare.
    # Days-back per granularity bucket (used to define the "prior" comparison).
    bucket_days = {"Day": 1, "Week": 7, "Month": 30, "Quarter": 90}.get(granularity, 7)

    def _equivalent_prior_set(selected, days_offset):
        target_dates = [pd.Timestamp(p) - pd.Timedelta(days=days_offset) for p in selected]
        matched = []
        for tgt in target_dates:
            cand = [p for p in periods if abs((pd.Timestamp(p) - tgt).days) <= 3]
            if cand:
                matched.append(max(cand))
        return sorted(set(matched))

    prior_set = _equivalent_prior_set(selected_periods, bucket_days)
    if prior_set:
        prior_raw = mp_slice[mp_slice["Period"].isin(prior_set)]
        prior_agg = aggregate_periods(prior_raw)
        prior = prior_agg.set_index("SKU")["CM3%"]
        filtered["Δ CM3 vs prior"] = filtered["CM3%"] - filtered["SKU"].map(prior)
        delta_caption = (
            f"Δ CM3% compares to the equivalent prior {granularity.lower()} "
            f"(ending {_fmt_day(max(prior_set))})."
        )
    else:
        filtered["Δ CM3 vs prior"] = pd.NA
        delta_caption = f"No equivalent prior {granularity.lower()} available for Δ CM3%."

    # ----- Cluster tiers: computed on the aggregated marketplace slice -----
    mp_period = aggregate_periods(raw_slice) if needs_aggregation else raw_slice.copy()
    tier_base = mp_period.dropna(subset=["CM3%", "Product Sales"]).copy()
    tier_base["Margin Tier"] = tier_1_to_3(tier_base["CM3%"])
    tier_base["Volume Tier"] = tier_1_to_3(tier_base["Product Sales"])
    tier_base["Cluster Code"] = (
        tier_base["Margin Tier"].astype("string") + "-" + tier_base["Volume Tier"].astype("string")
    )
    tier_base["Cluster"] = tier_base["Cluster Code"].map(cluster_name)
    tier_lookup = tier_base.set_index("SKU")[["Margin Tier", "Volume Tier", "Cluster Code", "Cluster"]]
    filtered = filtered.join(tier_lookup, on="SKU")
    mp_period = mp_period.merge(
        tier_lookup, left_on="SKU", right_index=True, how="left"
    )

    # ----- Inventory columns: always use the LATEST period's values -----
    # FBA Available, Days of Supply, Sales Velocity represent current-state
    # inventory health; old snapshots are not useful when reviewing prior
    # periods, so we overwrite them with the latest values per SKU.
    inventory_cols = [c for c in ["FBA Available", "Days of Supply", "Sales Velocity"]
                      if c in mp_slice.columns]
    latest_mp_period = max(mp_slice["Period"].dropna().unique())
    latest_rows = mp_slice[mp_slice["Period"] == latest_mp_period].copy()
    # In 'All countries' mode the same SKU appears in several marketplaces, so
    # we must aggregate per SKU first. Sum FBA Available and Sales Velocity
    # (additive across warehouses); weighted-avg Days of Supply by FBA stock
    # so a near-empty warehouse doesn't drag the figure down.
    for col in ["FBA Available", "Sales Velocity"]:
        if col in latest_rows.columns:
            latest_rows[col] = pd.to_numeric(latest_rows[col], errors="coerce")
    inventory_latest = pd.DataFrame(index=latest_rows["SKU"].drop_duplicates())
    if "FBA Available" in inventory_cols:
        inventory_latest["FBA Available"] = (
            latest_rows.groupby("SKU")["FBA Available"].sum(min_count=1)
        )
    if "Sales Velocity" in inventory_cols:
        inventory_latest["Sales Velocity"] = (
            latest_rows.groupby("SKU")["Sales Velocity"].sum(min_count=1)
        )
    if "Days of Supply" in inventory_cols:
        dos_v = pd.to_numeric(latest_rows["Days of Supply"], errors="coerce")
        weights = pd.to_numeric(latest_rows.get("FBA Available"), errors="coerce")
        if weights is None or weights.isna().all():
            inventory_latest["Days of Supply"] = (
                latest_rows.groupby("SKU")["Days of Supply"].mean()
            )
        else:
            valid = dos_v.notna() & weights.notna() & (weights > 0)
            tmp = pd.DataFrame({
                "SKU": latest_rows["SKU"],
                "_v": (dos_v * weights).where(valid),
                "_w": weights.where(valid),
            })
            agg = tmp.groupby("SKU").agg(num=("_v", "sum"), den=("_w", "sum"))
            inventory_latest["Days of Supply"] = agg["num"] / agg["den"].replace(0, pd.NA)
    for col in inventory_cols:
        filtered[col] = filtered["SKU"].map(inventory_latest[col])
    inventory_caption = (
        f"Inventory columns ({', '.join(inventory_cols)}) always show the latest "
        f"snapshot ({_fmt_day(latest_mp_period)}), regardless of the selected period."
    )

    # ----- Revenue growth: same calendar weeks one month earlier (shift back 4) -----
    prior_4w_set = _equivalent_prior_set(selected_periods, 28)
    if prior_4w_set:
        cur_rev = filtered.set_index("SKU")["Product Sales"]
        prior_4w_raw = mp_slice[mp_slice["Period"].isin(prior_4w_set)]
        prior_4w_agg = aggregate_periods(prior_4w_raw)
        prev_rev = prior_4w_agg.set_index("SKU")["Product Sales"]
        # Align by SKU; some current SKUs may not appear in the prior set.
        wow4 = ((cur_rev - prev_rev.reindex(cur_rev.index))
                / prev_rev.reindex(cur_rev.index).replace(0, pd.NA)) * 100
        filtered["Rev Δ 4w %"] = filtered["SKU"].map(wow4)
        growth_caption = (
            f"Rev Δ 4w % compares the selection to the same days 4 weeks earlier "
            f"(ending {_fmt_day(max(prior_4w_set))})."
        )
    else:
        filtered["Rev Δ 4w %"] = pd.NA
        growth_caption = "No equivalent set 4 weeks back, so Rev Δ 4w % is empty."

    # ----- P&L Impact = total CM3 (absolute € contribution) -----
    # Daily export carries CM3 in € directly; weekly legacy schema only has the
    # percentage, so fall back to CM3% × Sales there.
    if "CM3" in filtered.columns:
        filtered["P&L Impact"] = pd.to_numeric(filtered["CM3"], errors="coerce")
    else:
        cm3_frac = pd.to_numeric(filtered.get("CM3%"), errors="coerce") / 100
        sales = pd.to_numeric(filtered.get("Product Sales"), errors="coerce")
        filtered["P&L Impact"] = cm3_frac * sales

    # ----- Per-country breakdown (only shown in 'All countries' mode) -----
    if is_all_countries:
        # Aggregate the selected weeks per marketplace.
        breakdown = []
        for mp_name in real_marketplaces:
            mp_rows = df[
                (df["Marketplace Name"] == mp_name)
                & (df["Period"].isin(selected_periods))
            ]
            if mp_rows.empty:
                continue
            agg = aggregate_periods(mp_rows) if len(selected_periods) > 1 else mp_rows
            sales_sum = pd.to_numeric(agg["Product Sales"], errors="coerce").sum()
            units_sum = pd.to_numeric(agg["Units"], errors="coerce").sum()
            spend_sum = pd.to_numeric(agg["Sponsored Spend"], errors="coerce").sum() if "Sponsored Spend" in agg.columns else 0.0
            # Country-level margin: total CM3 € / total Sales € — NOT a per-SKU average.
            if "CM3" in agg.columns:
                cm3_sum = pd.to_numeric(agg["CM3"], errors="coerce").sum()
            else:
                # Legacy: derive from CM3% × Sales row-by-row.
                cm3_pct = pd.to_numeric(agg["CM3%"], errors="coerce")
                sales_series = pd.to_numeric(agg["Product Sales"], errors="coerce")
                cm3_sum = ((cm3_pct / 100) * sales_series).sum()
            country_cm3_pct = (cm3_sum / sales_sum * 100) if sales_sum else pd.NA
            breakdown.append({
                "Marketplace": mp_name,
                "SKUs": int(agg["SKU"].nunique()),
                "Sales (€)": sales_sum,
                "Units": units_sum,
                "Country CM3 %": country_cm3_pct,
                "P&L Impact (€)": cm3_sum,
                "Ad spend (€)": spend_sum,
            })
        breakdown_df = pd.DataFrame(breakdown).sort_values("Sales (€)", ascending=False)
        if not breakdown_df.empty:
            tot = pd.DataFrame([{
                "Marketplace": "Total",
                "SKUs": breakdown_df["SKUs"].sum(),
                "Sales (€)": breakdown_df["Sales (€)"].sum(),
                "Units": breakdown_df["Units"].sum(),
                "Country CM3 %": (
                    breakdown_df["P&L Impact (€)"].sum() / breakdown_df["Sales (€)"].sum() * 100
                ) if breakdown_df["Sales (€)"].sum() else pd.NA,
                "P&L Impact (€)": breakdown_df["P&L Impact (€)"].sum(),
                "Ad spend (€)": breakdown_df["Ad spend (€)"].sum(),
            }])
            breakdown_df = pd.concat([breakdown_df, tot], ignore_index=True)

            st.markdown("**Per-country breakdown** — Country CM3 % = total CM3 € / total Sales € for that marketplace.")
            st.dataframe(
                breakdown_df.style.format({
                    "Sales (€)": "€{:,.0f}",
                    "P&L Impact (€)": "€{:,.0f}",
                    "Ad spend (€)": "€{:,.0f}",
                    "Country CM3 %": "{:.1f}%",
                    "Units": "{:,.0f}",
                    "SKUs": "{:,.0f}",
                }, na_rep="—").apply(
                    lambda row: ["font-weight:600; background:#F2F4F8" if row["Marketplace"] == "Total" else ""] * len(row),
                    axis=1,
                ),
                use_container_width=True,
                hide_index=True,
            )

            # Stacked bar: sales + P&L impact side by side per country (Total row excluded)
            chart_df = breakdown_df[breakdown_df["Marketplace"] != "Total"].copy()
            country_fig = go.Figure()
            country_fig.add_bar(
                x=chart_df["Marketplace"], y=chart_df["Sales (€)"], name="Sales (€)",
                marker_color="#1f3864",
            )
            country_fig.add_bar(
                x=chart_df["Marketplace"], y=chart_df["P&L Impact (€)"], name="P&L Impact (€)",
                marker_color="#74AC2A",
            )
            country_fig.update_layout(
                barmode="group", title="Sales vs P&L Impact per country",
                yaxis_title="€", height=320, margin=dict(t=40, b=20, l=10, r=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(country_fig, use_container_width=True)

    # ----- Cluster matrix (clickable 3x3 button grid) -----
    st.markdown(
        "**Margin × Volume clusters** — Tier 1 = top third, Tier 3 = bottom third. "
        "*Click a cell to filter the table below.*"
    )

    cluster_grid = (
        tier_base.groupby(["Margin Tier", "Volume Tier"], observed=True)
        .size()
        .unstack(fill_value=0)
        .reindex(index=[1, 2, 3], columns=[1, 2, 3], fill_value=0)
    )

    if "active_cluster_code" not in st.session_state:
        st.session_state["active_cluster_code"] = None

    if cluster_grid.values.sum() > 0:
        sales_labels = {1: "High sales", 2: "Mid sales", 3: "Low sales"}
        margin_labels = {1: "High margin", 2: "Mid margin", 3: "Low margin"}

        # Same color map as the Cluster cells in the main table.
        cluster_bg = {
            "1-1": ("#C6EFCE", True),   # Star — bold green
            "1-2": ("#E2F0D9", False),
            "1-3": ("#E2F0D9", False),
            "2-1": ("#DEEBF7", False),
            "2-2": ("#FFFFFF", False),
            "2-3": ("#FFFFFF", False),
            "3-1": ("#DEEBF7", False),
            "3-2": ("#FFFFFF", False),
            "3-3": ("#F8CBAD", False),  # Dog — orange
        }

        # CSS: paint each button by the container key (st.container(key=...) emits
        # an 'st-key-<key>' class on the wrapping div in Streamlit ≥ 1.37).
        css_rules = []
        for code, (bg, bold) in cluster_bg.items():
            cls = f"st-key-cell_{code.replace('-', '_')}"
            weight = "font-weight:600;" if bold else ""
            css_rules.append(
                f".{cls} button {{ background:{bg} !important; color:#111 !important; "
                f"border:1px solid #d6d8dc !important; {weight} height:64px !important; "
                f"font-size:1rem !important; }}"
            )
        active_code = st.session_state["active_cluster_code"]
        if active_code:
            cls = f"st-key-cell_{active_code.replace('-', '_')}"
            css_rules.append(
                f".{cls} button {{ outline:3px solid #1f3864 !important; "
                f"outline-offset:-3px; }}"
            )
        st.markdown(f"<style>{''.join(css_rules)}</style>", unsafe_allow_html=True)

        header_cols = st.columns([1.4, 2, 2, 2], gap="small")
        header_cols[0].markdown("&nbsp;", unsafe_allow_html=True)
        for i, v in enumerate([1, 2, 3]):
            header_cols[i + 1].markdown(
                f"<div style='text-align:center; font-weight:600; padding:6px 0;'>{sales_labels[v]}</div>",
                unsafe_allow_html=True,
            )

        for m in [1, 2, 3]:
            row_cols = st.columns([1.4, 2, 2, 2], gap="small")
            row_cols[0].markdown(
                f"<div style='font-weight:600; padding:22px 0;'>{margin_labels[m]}</div>",
                unsafe_allow_html=True,
            )
            for i, v in enumerate([1, 2, 3]):
                code = f"{m}-{v}"
                count = int(cluster_grid.loc[m, v])
                badge = " ⭐" if (m == 1 and v == 1) else (" ⚠️" if (m == 3 and v == 3) else "")
                is_active = (active_code == code)
                label = f"{count} SKUs{badge}"
                with row_cols[i + 1].container(key=f"cell_{m}_{v}"):
                    if st.button(
                        label,
                        key=f"cluster_btn_{code}",
                        use_container_width=True,
                        help=cluster_name(code) + (" — active filter" if is_active else ""),
                    ):
                        st.session_state["active_cluster_code"] = None if is_active else code
                        st.rerun()

    clicked_cluster = st.session_state.get("active_cluster_code")

    # ----- Active cluster filter (drives the main table below) -----
    # Click on the heatmap takes precedence; the multiselect is the alternate
    # entry point.
    available_codes = [
        c for c in mp_period.get("Cluster Code", pd.Series(dtype="string")).dropna().unique()
        if c and "<NA>" not in c
    ]
    cluster_options = [c for c in CLUSTER_NAMES if c in available_codes]

    if clicked_cluster:
        active_clusters = [clicked_cluster]
    else:
        cluster_pick = st.multiselect(
            "Filter by cluster (or click a cell above)",
            options=cluster_options,
            default=[],
            format_func=lambda c: f"{cluster_name(c)}  ({c})",
            help="High margin · High sales = top-third in both. Low margin · Low sales = bottom-third in both.",
        )
        active_clusters = cluster_pick

    if active_clusters:
        # Active-filter badge with a clear-button when a cell was clicked.
        labels = ", ".join(f"**{cluster_name(c)}** ({c})" for c in active_clusters)
        if clicked_cluster:
            bcol, ccol = st.columns([6, 1])
            bcol.info(f"Filtering by: {labels} — main table below is filtered to these SKUs.")
            if ccol.button("Clear", key="clear_cluster_filter"):
                st.session_state["active_cluster_code"] = None
                st.rerun()
        else:
            st.info(f"Filtering by: {labels}")
        filtered = filtered[filtered["Cluster Code"].isin(active_clusters)]

        # Quick KPI tiles for the active cluster set.
        d1, d2, d3, d4, d5 = st.columns(5)
        d1.metric("SKUs in cluster", f"{len(filtered):,}")
        d2.metric("Total sales (€)", f"{filtered['Product Sales'].sum():,.0f}")
        avg_cm3_c = filtered["CM3%"].mean()
        d3.metric("Avg CM3 %", f"{avg_cm3_c:.1f}" if pd.notna(avg_cm3_c) else "—")
        d4.metric("Total units", f"{filtered['Units'].sum():,.0f}")
        avg_mom_c = filtered["Rev Δ 4w %"].mean() if "Rev Δ 4w %" in filtered.columns else pd.NA
        d5.metric("Avg Rev Δ 4w %", f"{avg_mom_c:+.1f}%" if pd.notna(avg_mom_c) else "—")

    below_target_only = st.checkbox(
        f"Show only SKUs with CM3% below {target_cm3:.1f}%", value=False
    )
    if below_target_only:
        filtered = filtered[filtered["CM3%"] < target_cm3]

    # ----- Per-SKU comments (loaded from comments.json) -----
    if "comments" not in st.session_state:
        st.session_state["comments"] = load_comments()
    filtered["Comments"] = filtered["SKU"].map(st.session_state["comments"]).fillna("")

    display_cols = [
        "SKU", "Product", "Comments", "Cluster", "Margin Tier", "Volume Tier",
        "Orders", "Units", "Product Sales", "Rev Δ 4w %",
        "CM1%", "CM2%", "CM3%", "Δ CM3 vs prior", "P&L Impact",
        "Sponsored Spend", "ROAS", "CTR",
        "FBA Available", "Days of Supply", "Sales Velocity",
        "Child ASIN",
    ]
    display_cols = [c for c in display_cols if c in filtered.columns]
    table = filtered[display_cols].sort_values(
        "Product Sales", ascending=False, na_position="last"
    )

    st.caption(delta_caption + " · " + growth_caption + " · " + inventory_caption)

    # --- Unified editable table with conditional cell formatting via AgGrid ---
    from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode

    fmt_euro = JsCode("function(p){return p.value==null?'—':'€'+Number(p.value).toLocaleString('de-DE',{maximumFractionDigits:0});}")
    fmt_int = JsCode("function(p){return p.value==null?'—':Number(p.value).toLocaleString('de-DE',{maximumFractionDigits:0});}")
    fmt_pct = JsCode("function(p){return p.value==null?'—':Number(p.value).toFixed(1)+'%';}")
    fmt_pct_signed = JsCode("function(p){if(p.value==null)return '—';var s=p.value>=0?'+':'';return s+Number(p.value).toFixed(1)+'%';}")
    fmt_pp = JsCode("function(p){if(p.value==null)return '—';var s=p.value>=0?'+':'';return s+Number(p.value).toFixed(1)+' pp';}")
    fmt_float2 = JsCode("function(p){return p.value==null?'—':Number(p.value).toFixed(2);}")
    fmt_float1 = JsCode("function(p){return p.value==null?'—':Number(p.value).toFixed(1);}")

    style_cm3 = JsCode(
        f"function(p){{if(p.value==null)return null;"
        f"if(p.value<{target_cm3})return{{'backgroundColor':'#F8CBAD','color':'#111'}};"
        f"return{{'backgroundColor':'#C6EFCE','color':'#111'}};}}"
    )
    style_delta_text = JsCode(
        "function(p){if(p.value==null)return null;"
        "if(p.value<0)return{'color':'#B71C1C','fontWeight':'600'};"
        "if(p.value>0)return{'color':'#1B5E20','fontWeight':'600'};return null;}"
    )
    style_dos = JsCode(
        f"function(p){{if(p.value==null)return null;"
        f"if(p.value<{min_dos})return{{'backgroundColor':'#F8CBAD','color':'#111'}};return null;}}"
    )
    style_cluster = JsCode(
        "function(p){var c=p.data && p.data['Cluster Code'];"
        "if(!c||c.indexOf('NA')>-1)return null;"
        "var parts=c.split('-');var m=parts[0],v=parts[1];"
        "if(m=='1'&&v=='1')return{'backgroundColor':'#C6EFCE','fontWeight':'600','color':'#111'};"
        "if(m=='3'&&v=='3')return{'backgroundColor':'#F8CBAD','color':'#111'};"
        "if(m=='1')return{'backgroundColor':'#E2F0D9','color':'#111'};"
        "if(v=='1')return{'backgroundColor':'#DEEBF7','color':'#111'};"
        "return null;}"
    )

    # Pass Cluster Code through so the cluster styler can read it (hidden column).
    table_for_grid = table.copy()
    if "Cluster Code" in filtered.columns and "Cluster Code" not in table_for_grid.columns:
        table_for_grid["Cluster Code"] = filtered.set_index("SKU")["Cluster Code"].reindex(
            table_for_grid["SKU"].values
        ).values

    gb = GridOptionsBuilder.from_dataframe(table_for_grid)
    gb.configure_default_column(
        editable=False, resizable=True, sortable=True, filter=True,
        cellStyle={"color": "#111"},
    )

    # Per-column formatting + conditional styling
    column_specs = {
        "SKU": dict(width=120, pinned="left"),
        "Product": dict(width=260, pinned="left", tooltipField="Product"),
        "Comments": dict(editable=True, width=220,
                         headerName="Comments ✏",
                         cellStyle={"backgroundColor": "#FFFDE7", "color": "#111"}),
        "Cluster": dict(width=170, cellStyle=style_cluster),
        "Cluster Code": dict(hide=True),
        "Margin Tier": dict(width=70, type=["numericColumn"]),
        "Volume Tier": dict(width=70, type=["numericColumn"]),
        "Orders": dict(width=80, type=["numericColumn"], valueFormatter=fmt_int),
        "Units": dict(width=80, type=["numericColumn"], valueFormatter=fmt_int),
        "Product Sales": dict(width=110, type=["numericColumn"], valueFormatter=fmt_euro),
        "Rev Δ 4w %": dict(width=110, type=["numericColumn"], valueFormatter=fmt_pct_signed, cellStyle=style_delta_text),
        "CM1%": dict(width=80, type=["numericColumn"], valueFormatter=fmt_pct),
        "CM2%": dict(width=80, type=["numericColumn"], valueFormatter=fmt_pct),
        "CM3%": dict(width=80, type=["numericColumn"], valueFormatter=fmt_pct, cellStyle=style_cm3),
        "Δ CM3 vs prior": dict(width=110, type=["numericColumn"], valueFormatter=fmt_pp, cellStyle=style_delta_text),
        "P&L Impact": dict(width=110, type=["numericColumn"], valueFormatter=fmt_euro),
        "Sponsored Spend": dict(width=110, type=["numericColumn"], valueFormatter=fmt_euro),
        "ROAS": dict(width=80, type=["numericColumn"], valueFormatter=fmt_float2),
        "CTR": dict(width=80, type=["numericColumn"], valueFormatter=fmt_float2),
        "FBA Available": dict(width=110, type=["numericColumn"], valueFormatter=fmt_int),
        "Days of Supply": dict(width=110, type=["numericColumn"], valueFormatter=fmt_int, cellStyle=style_dos),
        "Sales Velocity": dict(width=110, type=["numericColumn"], valueFormatter=fmt_float1),
        "Child ASIN": dict(width=120),
    }
    for col, spec in column_specs.items():
        if col in table_for_grid.columns:
            gb.configure_column(col, **spec)

    grid_options = gb.build()
    grid_response = AgGrid(
        table_for_grid,
        gridOptions=grid_options,
        allow_unsafe_jscode=True,
        update_mode=GridUpdateMode.VALUE_CHANGED,
        fit_columns_on_grid_load=False,
        height=600,
        theme="streamlit",
        reload_data=False,
        key=f"main_grid_{marketplace}_{period}_{granularity}",
    )

    edited = pd.DataFrame(grid_response["data"]) if grid_response and "data" in grid_response else table_for_grid

    # Persist any comment changes back to comments.json / session.
    new_comments = dict(st.session_state["comments"])
    changed = False
    if "Comments" in edited.columns and "SKU" in edited.columns:
        for sku, comment in zip(edited["SKU"], edited["Comments"]):
            comment_str = (str(comment) if comment is not None else "").strip()
            prev = new_comments.get(sku, "")
            if comment_str != prev:
                if comment_str:
                    new_comments[sku] = comment_str
                elif sku in new_comments:
                    del new_comments[sku]
                changed = True
    if changed:
        st.session_state["comments"] = new_comments
        try:
            save_comments(new_comments)
        except Exception as exc:
            st.warning(f"Couldn't write comments.json ({exc}); kept in session only.")

    dl_col1, dl_col2 = st.columns([1, 1])
    dl_col1.download_button(
        "Download filtered rows (CSV)",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name=f"margin_{marketplace}_{pd.Timestamp(period):%Y%m%d}.csv",
        mime="text/csv",
    )
    dl_col2.download_button(
        "Download comments (JSON)",
        data=json.dumps(st.session_state["comments"], indent=2, ensure_ascii=False).encode("utf-8"),
        file_name="comments.json",
        mime="application/json",
        help="Comments persist on the server but reset on each redeploy. Commit this file to keep them permanently.",
    )

# =========================================================================
# Compare tab — bubble chart for the selected period
# =========================================================================
with tab_compare:
    scatter_src = filtered.dropna(subset=["Product Sales", "CM3%"]).copy()
    if scatter_src.empty:
        st.info("No rows with both Product Sales and CM3% to plot.")
    else:
        scatter_src["Top Seller"] = (
            scatter_src[top_col].fillna("").eq("Top Seller").map({True: "Top Seller", False: "Other"})
            if top_col and top_col in scatter_src.columns
            else "Other"
        )
        fig = px.scatter(
            scatter_src,
            x="Product Sales",
            y="CM3%",
            size="Units",
            color="Top Seller",
            color_discrete_map={"Top Seller": "#1f3864", "Other": "#bdbdbd"},
            hover_name="SKU",
            hover_data={"Product": True, "Orders": True, "ROAS": ":.2f", "Units": True},
            size_max=40,
            title=f"Sales vs CM3% — {marketplace} @ {pd.Timestamp(period):%Y-%m-%d}",
        )
        fig.update_yaxes(ticksuffix="%")
        fig.update_xaxes(tickprefix="€")
        fig.add_hline(
            y=target_cm3, line_dash="dot", line_color="#d32f2f",
            annotation_text=f"Target CM3 {target_cm3:.1f}%",
            annotation_position="top right",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "Bubble size = Units sold. SKUs below the dotted line are missing the CM3% target."
        )
