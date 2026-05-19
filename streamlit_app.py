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
    period = c2.selectbox(
        "Period",
        periods,
        format_func=lambda d: pd.Timestamp(d).strftime("%Y-%m-%d"),
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
        delta_caption = (
            f"Δ CM3% column compares to {pd.Timestamp(prior_period):%Y-%m-%d}."
        )
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
    tier_base["Cluster"] = (
        tier_base["Margin Tier"].astype("string") + "-" + tier_base["Volume Tier"].astype("string")
    )
    tier_lookup = tier_base.set_index("SKU")[["Margin Tier", "Volume Tier", "Cluster"]]
    filtered = filtered.join(tier_lookup, on="SKU")
    mp_period = mp_period.merge(
        tier_lookup, left_on="SKU", right_index=True, how="left"
    )

    # ----- MoM Revenue %: current calendar month vs previous calendar month -----
    sku_monthly = (
        mp_slice.dropna(subset=["Period"])
        .assign(Month=lambda d: d["Period"].dt.to_period("M"))
        .groupby(["SKU", "Month"], as_index=False)["Product Sales"]
        .sum()
    )
    current_month = pd.Timestamp(period).to_period("M")
    previous_month = current_month - 1
    cur = sku_monthly[sku_monthly["Month"] == current_month].set_index("SKU")["Product Sales"]
    prev = sku_monthly[sku_monthly["Month"] == previous_month].set_index("SKU")["Product Sales"]
    mom = ((cur - prev) / prev.replace(0, pd.NA)) * 100
    filtered["MoM Rev %"] = filtered["SKU"].map(mom)
    weeks_in_current = (
        mp_slice.dropna(subset=["Period"])
        .loc[lambda d: d["Period"].dt.to_period("M") == current_month, "Period"]
        .nunique()
    )
    partial_note = (
        f" Note: {current_month.strftime('%b %Y')} contains only {weeks_in_current} "
        f"week(s) of data so far — current month is partial."
        if weeks_in_current < 4 else ""
    )
    mom_caption = (
        f"MoM Rev % compares {current_month.strftime('%b %Y')} revenue to "
        f"{previous_month.strftime('%b %Y')} (calendar months, same SKU, same marketplace)."
        + partial_note
    )

    # ----- Cluster distribution chart -----
    st.markdown("**Margin × Volume clusters** (Tier 1 = top third, Tier 3 = bottom third)")
    grid = (
        mp_period.dropna(subset=["Margin Tier", "Volume Tier"])
        .groupby(["Margin Tier", "Volume Tier"], observed=True)
        .size()
        .reset_index(name="SKUs")
    )
    clicked_cluster = None
    if not grid.empty:
        cluster_grid = (
            tier_base.groupby(["Margin Tier", "Volume Tier"], observed=True)
            .size()
            .unstack(fill_value=0)
            .reindex(index=[1, 2, 3], columns=[1, 2, 3], fill_value=0)
        )
        cluster_fig = go.Figure(
            data=go.Heatmap(
                z=cluster_grid.values,
                x=["1 (high)", "2 (mid)", "3 (low)"],
                y=["1 (high)", "2 (mid)", "3 (low)"],
                text=cluster_grid.values,
                texttemplate="%{text}",
                textfont=dict(size=18, color="#111"),
                colorscale="Blues",
                showscale=False,
                hovertemplate="Margin Tier %{y}<br>Volume Tier %{x}<br><b>%{z} SKUs</b><extra></extra>",
                xgap=3, ygap=3,
            )
        )
        cluster_fig.update_layout(
            xaxis=dict(title="Volume Tier (sales)", side="bottom"),
            yaxis=dict(title="Margin Tier (CM3%)", autorange="reversed"),
            height=340,
            margin=dict(t=10, b=10, l=10, r=10),
        )
        st.caption("Click any cell to see the products in that cluster.")
        event = st.plotly_chart(
            cluster_fig,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            key="cluster_heatmap",
        )
        sel = getattr(event, "selection", None) if event is not None else None
        points = []
        if sel is not None:
            points = sel.get("points", []) if isinstance(sel, dict) else (getattr(sel, "points", []) or [])
        if points:
            pt = points[0]
            try:
                sel_v = int(str(pt.get("x"))[0])
                sel_m = int(str(pt.get("y"))[0])
                clicked_cluster = f"{sel_m}-{sel_v}"
            except (TypeError, ValueError):
                clicked_cluster = None

    # ----- Drill-in panel for the clicked cluster -----
    if clicked_cluster:
        drill = filtered[filtered["Cluster"] == clicked_cluster].copy()
        # Fall back to the tier base if the cluster was filtered out upstream.
        if drill.empty:
            drill = tier_base[tier_base["Cluster"] == clicked_cluster].copy()
            drill["MoM Rev %"] = drill["SKU"].map(mom)
        drill = drill.sort_values("Product Sales", ascending=False, na_position="last")

        with st.container(border=True):
            margin_label = {"1": "high margin", "2": "mid margin", "3": "low margin"}[clicked_cluster[0]]
            volume_label = {"1": "high volume", "2": "mid volume", "3": "low volume"}[clicked_cluster[-1]]
            st.markdown(
                f"### Cluster `{clicked_cluster}` — {margin_label}, {volume_label}"
            )
            d1, d2, d3, d4, d5 = st.columns(5)
            d1.metric("SKUs", f"{len(drill):,}")
            d2.metric("Total sales (€)", f"{drill['Product Sales'].sum():,.0f}")
            d3.metric("Avg CM3 %", f"{drill['CM3%'].mean():.1f}" if pd.notna(drill['CM3%'].mean()) else "—")
            d4.metric("Total units", f"{drill['Units'].sum():,.0f}")
            avg_mom = drill["MoM Rev %"].mean() if "MoM Rev %" in drill.columns else None
            d5.metric("Avg MoM Rev %", f"{avg_mom:+.1f}%" if pd.notna(avg_mom) else "—")

            drill_cols = [
                "SKU", "Product", "Product Sales", "Units", "Orders",
                "CM3%", "MoM Rev %", "ROAS", "Sponsored Spend",
                "Days of Supply", "Sales Velocity",
            ]
            drill_cols = [c for c in drill_cols if c in drill.columns]
            drill_styled = drill[drill_cols].style.format(
                {
                    "Product Sales": "€{:,.0f}",
                    "Sponsored Spend": "€{:,.0f}",
                    "CM3%": "{:.1f}%",
                    "MoM Rev %": "{:+.1f}%",
                    "ROAS": "{:.2f}",
                    "Days of Supply": "{:,.0f}",
                    "Sales Velocity": "{:,.1f}",
                    "Orders": "{:,.0f}",
                    "Units": "{:,.0f}",
                },
                na_rep="—",
            )
            st.dataframe(drill_styled, use_container_width=True, hide_index=True, height=320)

            st.download_button(
                f"Download cluster {clicked_cluster} (CSV)",
                data=drill[drill_cols].to_csv(index=False).encode("utf-8"),
                file_name=f"cluster_{clicked_cluster}_{marketplace}_{pd.Timestamp(period):%Y%m%d}.csv",
                mime="text/csv",
                key="dl_cluster",
            )

    # ----- Cluster filter -----
    cluster_options = sorted(
        [c for c in mp_period["Cluster"].dropna().unique() if c and "<NA>" not in c]
    )
    cluster_pick = st.multiselect(
        "Filter by cluster (margin tier – volume tier)",
        options=cluster_options,
        default=[],
        help="1-1 = top-third margin AND top-third sales. 3-3 = bottom-third in both.",
    )
    if cluster_pick:
        filtered = filtered[filtered["Cluster"].isin(cluster_pick)]

    below_target_only = st.checkbox(
        f"Show only SKUs with CM3% below {target_cm3:.1f}%", value=False
    )
    if below_target_only:
        filtered = filtered[filtered["CM3%"] < target_cm3]

    display_cols = [
        "SKU", "Product", "Cluster", "Margin Tier", "Volume Tier",
        "Orders", "Units", "Product Sales", "MoM Rev %",
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
                "MoM Rev %": "{:+.1f}%",
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
        if "MoM Rev %" in df_.columns:
            styled = styled.map(
                lambda v: "color: #B71C1C" if pd.notna(v) and v < 0
                else ("color: #1B5E20" if pd.notna(v) and v > 0 else ""),
                subset=["MoM Rev %"],
            )
        if "Cluster" in df_.columns:
            def _cluster_bg(v):
                if not isinstance(v, str) or "-" not in v or "NA" in v:
                    return ""
                m, vol = v.split("-")
                # Star (high margin + high volume) → green. Dog (3-3) → red.
                if m == "1" and vol == "1":
                    return "background-color: #C6EFCE; font-weight: 600"
                if m == "3" and vol == "3":
                    return "background-color: #F8CBAD"
                if m == "1":
                    return "background-color: #E2F0D9"
                if vol == "1":
                    return "background-color: #DEEBF7"
                return ""
            styled = styled.map(_cluster_bg, subset=["Cluster"])
        if "Days of Supply" in df_.columns:
            styled = styled.map(
                lambda v: "background-color: #F8CBAD" if pd.notna(v) and v < min_dos else "",
                subset=["Days of Supply"],
            )
        return styled

    st.caption(delta_caption + " · " + mom_caption)
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

            # Monthly revenue + MoM growth
            monthly = monthly_revenue(sku_hist)
            if len(monthly) >= 1:
                mom_fig = go.Figure()
                mom_fig.add_bar(
                    x=monthly["Month"],
                    y=monthly["Product Sales"],
                    name="Revenue (€)",
                    marker_color="#1f3864",
                    yaxis="y1",
                )
                mom_fig.add_scatter(
                    x=monthly["Month"],
                    y=monthly["MoM %"],
                    name="MoM growth (%)",
                    mode="lines+markers+text",
                    text=[f"{v:+.1f}%" if pd.notna(v) else "" for v in monthly["MoM %"]],
                    textposition="top center",
                    line=dict(color="#d32f2f", width=2),
                    marker=dict(size=8),
                    yaxis="y2",
                )
                mom_fig.update_layout(
                    title="Monthly revenue + MoM growth",
                    xaxis=dict(title="Month", tickformat="%b %Y"),
                    yaxis=dict(title="Revenue (€)", side="left"),
                    yaxis2=dict(
                        title="MoM %", overlaying="y", side="right",
                        ticksuffix="%", zeroline=True, zerolinecolor="#bbb",
                    ),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02),
                )
                st.plotly_chart(mom_fig, use_container_width=True)

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
