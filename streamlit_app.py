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
    candidates = sorted(exports_dir.glob("margin_export_*.csv"))
    return candidates[-1] if candidates else None


@st.cache_data(show_spinner=False)
def load_data(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["Period"] = pd.to_datetime(df["Period"], errors="coerce")
    numeric_cols = [
        "CM1%", "CM2%", "CM3%", "Sponsored Spend", "ROAS", "CTR",
        "Orders", "Units", "Product Sales",
        "FBA Available", "Days of Supply", "Sales Velocity",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
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


def monthly_revenue(history: pd.DataFrame) -> pd.DataFrame:
    """Aggregate Product Sales by calendar month for one SKU's history."""
    if history.empty or "Period" not in history.columns:
        return pd.DataFrame(columns=["Month", "Product Sales", "MoM %"])
    monthly = (
        history.dropna(subset=["Period"])
        .assign(Month=lambda d: d["Period"].dt.to_period("M").dt.to_timestamp())
        .groupby("Month", as_index=False)["Product Sales"]
        .sum()
        .sort_values("Month")
    )
    monthly["MoM %"] = monthly["Product Sales"].pct_change() * 100
    return monthly


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
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 2, 1])
    marketplaces = sorted(df["Marketplace Name"].dropna().unique().tolist())
    default_mp = "amazon.de" if "amazon.de" in marketplaces else marketplaces[0]
    marketplace = c1.selectbox("Marketplace", marketplaces, index=marketplaces.index(default_mp))

    periods = sorted(df["Period"].dropna().unique(), reverse=True)

    def _fmt_week(d):
        ts = pd.Timestamp(d)
        iso = ts.isocalendar()
        return f"KW {iso.week:02d} · {iso.year}"

    period = c2.selectbox(
        "Calendar week",
        periods,
        format_func=_fmt_week,
    )

    sku_query = c3.text_input("SKU or Product contains", "")
    top_only = c4.toggle("Top sellers only", value=False)

# Marketplace + period base slice
mp_slice = df[df["Marketplace Name"] == marketplace].copy()
filtered = mp_slice[mp_slice["Period"] == period].copy()

if sku_query.strip():
    q = sku_query.strip().lower()
    filtered = filtered[
        filtered["SKU"].astype(str).str.lower().str.contains(q, na=False)
        | filtered["Product"].astype(str).str.lower().str.contains(q, na=False)
    ]

top_col = TOP_SELLER_COL.get(marketplace)
if top_only:
    if top_col and top_col in filtered.columns:
        filtered = filtered[filtered[top_col] == "Top Seller"]
    else:
        st.info(f"No top-seller flag column for {marketplace}; ignoring filter.")

# ----- KPIs -----
k1, k2, k3, k4 = st.columns(4)
k1.metric("SKUs in view", f"{len(filtered):,}")
k2.metric("Total sales (€)", f"{filtered['Product Sales'].sum():,.0f}")
avg_cm3 = filtered["CM3%"].mean()
k3.metric("Avg CM3 %", f"{avg_cm3:,.1f}" if pd.notna(avg_cm3) else "—")
below = int((filtered["CM3%"] < target_cm3).sum())
k4.metric(f"SKUs below {target_cm3:.0f}% CM3", f"{below:,}")

tab_overview, tab_trend, tab_compare = st.tabs(["Overview", "Trend", "Compare"])

# =========================================================================
# Overview tab — table + Δ vs previous period
# =========================================================================
with tab_overview:
    # ----- Δ CM3% vs previous period (same marketplace) -----
    prior_periods = [p for p in periods if p < period]
    prior_period = prior_periods[0] if prior_periods else None
    if prior_period is not None:
        prior = mp_slice[mp_slice["Period"] == prior_period].set_index("SKU")["CM3%"]
        filtered["Δ CM3 vs prior"] = filtered["CM3%"] - filtered["SKU"].map(prior)
        delta_caption = f"Δ CM3% column compares to {_fmt_week(prior_period)}."
    else:
        filtered["Δ CM3 vs prior"] = pd.NA
        delta_caption = "No prior period available for Δ CM3%."

    # ----- Cluster tiers: computed on rows that have BOTH CM3% and Product Sales
    # in the current marketplace+period, so both tiers are tertiles over the
    # same population (otherwise the 3x3 grid is lopsided).
    mp_period = mp_slice[mp_slice["Period"] == period].copy()
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
    inventory_latest = (
        mp_slice[mp_slice["Period"] == latest_mp_period]
        .set_index("SKU")[inventory_cols]
    )
    for col in inventory_cols:
        filtered[col] = filtered["SKU"].map(inventory_latest[col])
    inventory_caption = (
        f"Inventory columns ({', '.join(inventory_cols)}) always show the latest "
        f"snapshot ({_fmt_week(latest_mp_period)}), regardless of the selected week."
    )

    # ----- Revenue growth: same calendar week one month earlier (≈ 4 weeks back) -----
    period_ts = pd.Timestamp(period)
    target_prior = period_ts - pd.Timedelta(days=28)
    prior_4w_candidates = [
        p for p in periods if abs((pd.Timestamp(p) - target_prior).days) <= 3
    ]
    prior_4w = max(prior_4w_candidates) if prior_4w_candidates else None
    if prior_4w is not None:
        cur_rev = mp_slice[mp_slice["Period"] == period].set_index("SKU")["Product Sales"]
        prev_rev = mp_slice[mp_slice["Period"] == prior_4w].set_index("SKU")["Product Sales"]
        wow4 = ((cur_rev - prev_rev) / prev_rev.replace(0, pd.NA)) * 100
        filtered["Rev Δ 4w %"] = filtered["SKU"].map(wow4)
        growth_caption = (
            f"Rev Δ 4w % compares {_fmt_week(period)} revenue to "
            f"{_fmt_week(prior_4w)} (the same calendar week one month earlier)."
        )
    else:
        filtered["Rev Δ 4w %"] = pd.NA
        growth_caption = "No week 4 weeks back available, so Rev Δ 4w % is empty."

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

        header_cols = st.columns([1.4, 2, 2, 2], gap="small")
        header_cols[0].markdown("&nbsp;", unsafe_allow_html=True)
        for i, v in enumerate([1, 2, 3]):
            header_cols[i + 1].markdown(
                f"<div style='text-align:center; font-weight:600; padding:6px 0;'>{sales_labels[v]}</div>",
                unsafe_allow_html=True,
            )

        active_code = st.session_state["active_cluster_code"]
        for m in [1, 2, 3]:
            row_cols = st.columns([1.4, 2, 2, 2], gap="small")
            row_cols[0].markdown(
                f"<div style='font-weight:600; padding:14px 0;'>{margin_labels[m]}</div>",
                unsafe_allow_html=True,
            )
            for i, v in enumerate([1, 2, 3]):
                code = f"{m}-{v}"
                count = int(cluster_grid.loc[m, v])
                badge = " ⭐" if (m == 1 and v == 1) else (" ⚠️" if (m == 3 and v == 3) else "")
                is_active = (active_code == code)
                label = f"{'● ' if is_active else ''}{count} SKUs{badge}"
                btype = "primary" if is_active else "secondary"
                if row_cols[i + 1].button(
                    label,
                    key=f"cluster_btn_{code}",
                    use_container_width=True,
                    type=btype,
                    help=cluster_name(code),
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

    display_cols = [
        "SKU", "Product", "Cluster", "Margin Tier", "Volume Tier",
        "Orders", "Units", "Product Sales", "Rev Δ 4w %",
        "CM1%", "CM2%", "CM3%", "Δ CM3 vs prior",
        "Sponsored Spend", "ROAS", "CTR",
        "FBA Available", "Days of Supply", "Sales Velocity",
        "Child ASIN",
    ]
    display_cols = [c for c in display_cols if c in filtered.columns]
    table = filtered[display_cols].sort_values(
        "Product Sales", ascending=False, na_position="last"
    )

    def _style(df_: pd.DataFrame):
        styled = df_.style.format(
            {
                "Product Sales": "€{:,.0f}",
                "Sponsored Spend": "€{:,.0f}",
                "CM1%": "{:.1f}%",
                "CM2%": "{:.1f}%",
                "CM3%": "{:.1f}%",
                "Δ CM3 vs prior": "{:+.1f} pp",
                "Rev Δ 4w %": "{:+.1f}%",
                "ROAS": "{:.2f}",
                "CTR": "{:.2f}%",
                "FBA Available": "{:,.0f}",
                "Days of Supply": "{:,.0f}",
                "Sales Velocity": "{:,.1f}",
                "Orders": "{:,.0f}",
                "Units": "{:,.0f}",
            },
            na_rep="—",
        )
        if "CM3%" in df_.columns:
            styled = styled.map(
                lambda v: "background-color: #F8CBAD" if pd.notna(v) and v < target_cm3
                else ("background-color: #C6EFCE" if pd.notna(v) else ""),
                subset=["CM3%"],
            )
        if "Δ CM3 vs prior" in df_.columns:
            styled = styled.map(
                lambda v: "color: #B71C1C" if pd.notna(v) and v < 0
                else ("color: #1B5E20" if pd.notna(v) and v > 0 else ""),
                subset=["Δ CM3 vs prior"],
            )
        if "Rev Δ 4w %" in df_.columns:
            styled = styled.map(
                lambda v: "color: #B71C1C" if pd.notna(v) and v < 0
                else ("color: #1B5E20" if pd.notna(v) and v > 0 else ""),
                subset=["Rev Δ 4w %"],
            )
        if "Cluster" in df_.columns and "Margin Tier" in df_.columns and "Volume Tier" in df_.columns:
            def _cluster_bg(row):
                m, vol = row["Margin Tier"], row["Volume Tier"]
                if pd.isna(m) or pd.isna(vol):
                    return [""] * len(row)
                bg = ""
                if m == 1 and vol == 1:
                    bg = "background-color: #C6EFCE; font-weight: 600"
                elif m == 3 and vol == 3:
                    bg = "background-color: #F8CBAD"
                elif m == 1:
                    bg = "background-color: #E2F0D9"
                elif vol == 1:
                    bg = "background-color: #DEEBF7"
                return [bg if c == "Cluster" else "" for c in row.index]
            styled = styled.apply(_cluster_bg, axis=1)
        if "Days of Supply" in df_.columns:
            styled = styled.map(
                lambda v: "background-color: #F8CBAD" if pd.notna(v) and v < min_dos else "",
                subset=["Days of Supply"],
            )
        return styled

    st.caption(delta_caption + " · " + growth_caption + " · " + inventory_caption)
    st.dataframe(_style(table), use_container_width=True, hide_index=True, height=560)

    st.download_button(
        "Download filtered rows (CSV)",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name=f"margin_{marketplace}_{pd.Timestamp(period):%Y%m%d}.csv",
        mime="text/csv",
    )

# =========================================================================
# Trend tab — single SKU, all periods
# =========================================================================
with tab_trend:
    sku_pool = (
        mp_slice.dropna(subset=["SKU"])
        .sort_values("Product Sales", ascending=False)["SKU"]
        .drop_duplicates()
        .tolist()
    )
    if not sku_pool:
        st.info("No SKUs found for this marketplace.")
    else:
        default_sku = filtered["SKU"].iloc[0] if len(filtered) else sku_pool[0]
        default_idx = sku_pool.index(default_sku) if default_sku in sku_pool else 0
        sku = st.selectbox("SKU", sku_pool, index=default_idx, key="trend_sku")

        sku_hist = (
            mp_slice[mp_slice["SKU"] == sku]
            .sort_values("Period")
            .copy()
        )
        if sku_hist.empty:
            st.info("No history for this SKU.")
        else:
            product_name = sku_hist["Product"].dropna().iloc[-1] if sku_hist["Product"].notna().any() else ""
            st.markdown(f"**{sku}** — {product_name}")

            latest = sku_hist.iloc[-1]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Latest CM3 %", f"{latest['CM3%']:.1f}" if pd.notna(latest["CM3%"]) else "—")
            m2.metric("Latest sales (€)", f"{latest['Product Sales']:,.0f}" if pd.notna(latest["Product Sales"]) else "—")
            m3.metric("Latest units", f"{latest['Units']:,.0f}" if pd.notna(latest["Units"]) else "—")
            m4.metric("Days of supply", f"{latest['Days of Supply']:,.0f}" if pd.notna(latest["Days of Supply"]) else "—")

            # Margin trend
            margin_long = sku_hist.melt(
                id_vars=["Period"],
                value_vars=["CM1%", "CM2%", "CM3%"],
                var_name="Margin",
                value_name="Value",
            )
            fig = px.line(
                margin_long,
                x="Period",
                y="Value",
                color="Margin",
                markers=True,
                title="Margin % over time",
            )
            fig.update_yaxes(title="%", ticksuffix="%")
            fig.add_hline(y=target_cm3, line_dash="dot", line_color="#888",
                          annotation_text=f"Target CM3 {target_cm3:.1f}%",
                          annotation_position="top right")
            fig.add_hline(y=target_cm2, line_dash="dot", line_color="#bbb",
                          annotation_text=f"Target CM2 {target_cm2:.1f}%",
                          annotation_position="top right")
            fig.add_hline(y=target_cm1, line_dash="dot", line_color="#ddd",
                          annotation_text=f"Target CM1 {target_cm1:.1f}%",
                          annotation_position="top right")
            st.plotly_chart(fig, use_container_width=True)

            # Sales + spend (weekly)
            sales_fig = go.Figure()
            sales_fig.add_bar(
                x=sku_hist["Period"], y=sku_hist["Product Sales"], name="Sales (€)",
                marker_color="#1f3864",
            )
            sales_fig.add_bar(
                x=sku_hist["Period"], y=sku_hist["Sponsored Spend"], name="Ad spend (€)",
                marker_color="#f59e0b",
            )
            sales_fig.update_layout(
                barmode="group",
                title="Sales vs ad spend (weekly)",
                yaxis_title="€",
            )
            st.plotly_chart(sales_fig, use_container_width=True)

            # Weekly revenue + 4-week-ago growth (same calendar week, one month earlier)
            week_hist = sku_hist.sort_values("Period").copy()
            week_hist["Rev 4w ago"] = week_hist["Product Sales"].shift(4)
            week_hist["Δ 4w %"] = (
                (week_hist["Product Sales"] - week_hist["Rev 4w ago"])
                / week_hist["Rev 4w ago"].replace(0, pd.NA)
            ) * 100
            week_hist["KW"] = week_hist["Period"].apply(lambda d: _fmt_week(d))
            growth_fig = go.Figure()
            growth_fig.add_bar(
                x=week_hist["KW"],
                y=week_hist["Product Sales"],
                name="Revenue (€)",
                marker_color="#1f3864",
                yaxis="y1",
            )
            growth_fig.add_scatter(
                x=week_hist["KW"],
                y=week_hist["Δ 4w %"],
                name="Δ vs 4 weeks ago (%)",
                mode="lines+markers+text",
                text=[f"{v:+.0f}%" if pd.notna(v) else "" for v in week_hist["Δ 4w %"]],
                textposition="top center",
                line=dict(color="#d32f2f", width=2),
                marker=dict(size=7),
                yaxis="y2",
            )
            growth_fig.update_layout(
                title="Weekly revenue + Δ vs same week 4 weeks earlier",
                xaxis=dict(title="Calendar week"),
                yaxis=dict(title="Revenue (€)", side="left"),
                yaxis2=dict(
                    title="Δ 4w %", overlaying="y", side="right",
                    ticksuffix="%", zeroline=True, zerolinecolor="#bbb",
                ),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(growth_fig, use_container_width=True)

            with st.expander("Raw history rows"):
                st.dataframe(
                    sku_hist[[
                        "Period", "Orders", "Units", "Product Sales",
                        "CM1%", "CM2%", "CM3%",
                        "Sponsored Spend", "ROAS", "CTR",
                        "FBA Available", "Days of Supply", "Sales Velocity",
                    ]],
                    hide_index=True,
                    use_container_width=True,
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
