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
    # Build a Δ CM3% column vs the previous period (in the same marketplace).
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

    below_target_only = st.checkbox(
        f"Show only SKUs with CM3% below {target_cm3:.1f}%", value=False
    )
    if below_target_only:
        filtered = filtered[filtered["CM3%"] < target_cm3]

    display_cols = [
        "SKU", "Product", "Orders", "Units", "Product Sales",
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
        if "Days of Supply" in df_.columns:
            styled = styled.map(
                lambda v: "background-color: #F8CBAD" if pd.notna(v) and v < min_dos else "",
                subset=["Days of Supply"],
            )
        return styled

    st.caption(delta_caption)
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

            # Sales + spend
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
                title="Sales vs ad spend",
                yaxis_title="€",
            )
            st.plotly_chart(sales_fig, use_container_width=True)

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
