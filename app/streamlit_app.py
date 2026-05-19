"""Margin Analytics dashboard (Streamlit).

Reads the latest CSV in `novadata_exports/` (committed by the GitHub Actions
workflow `.github/workflows/weekly_export.yml`, which runs
`novadata_weekly_export.py --once`) and renders an interactive,
password-gated dashboard.

Run locally:
    pip install -r requirements.txt
    python novadata_weekly_export.py --once   # populate novadata_exports/
    export DASHBOARD_PASSWORD="choose-a-password"
    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
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


def top_seller_col_for(marketplace: str) -> str | None:
    return {
        "amazon.de": "DE filters",
        "amazon.co.uk": "UK filters",
        "amazon.fr": "FR filters",
        "amazon.es": "ES filters",
        "amazon.it": "IT filters",
    }.get(marketplace)


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

# ----- Filters -----
with st.container(border=True):
    st.subheader("Filters")
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

# ----- Apply filters -----
mask = (df["Marketplace Name"] == marketplace) & (df["Period"] == period)
filtered = df.loc[mask].copy()

if sku_query.strip():
    q = sku_query.strip().lower()
    filtered = filtered[
        filtered["SKU"].astype(str).str.lower().str.contains(q, na=False)
        | filtered["Product"].astype(str).str.lower().str.contains(q, na=False)
    ]

if top_only:
    flag_col = top_seller_col_for(marketplace)
    if flag_col and flag_col in filtered.columns:
        filtered = filtered[filtered[flag_col] == "Top Seller"]
    else:
        st.info(f"No top-seller flag column for {marketplace}; ignoring filter.")

# ----- Sidebar thresholds -----
target_cm3 = st.sidebar.number_input("Target CM3 %", value=19.7, step=0.5)
min_dos = st.sidebar.number_input("Min Days of Supply", value=30, step=5)
st.sidebar.caption("Adjust thresholds to recolor the table.")

# ----- KPIs -----
k1, k2, k3, k4 = st.columns(4)
k1.metric("SKUs in view", f"{len(filtered):,}")
k2.metric("Total sales (€)", f"{filtered['Product Sales'].sum():,.0f}")
avg_cm3 = filtered["CM3%"].mean()
k3.metric("Avg CM3 %", f"{avg_cm3:,.1f}" if pd.notna(avg_cm3) else "—")
below = (filtered["CM3%"] < target_cm3).sum()
k4.metric(f"SKUs below {target_cm3:.0f}% CM3", f"{int(below):,}")

# ----- Table -----
display_cols = [
    "SKU", "Product", "Orders", "Units", "Product Sales",
    "CM1%", "CM2%", "CM3%",
    "Sponsored Spend", "ROAS", "CTR",
    "FBA Available", "Days of Supply", "Sales Velocity",
    "Child ASIN",
]
display_cols = [c for c in display_cols if c in filtered.columns]
table = filtered[display_cols].sort_values("Product Sales", ascending=False, na_position="last")


def _style(df_: pd.DataFrame):
    styled = df_.style.format(
        {
            "Product Sales": "€{:,.0f}",
            "Sponsored Spend": "€{:,.0f}",
            "CM1%": "{:.1f}%",
            "CM2%": "{:.1f}%",
            "CM3%": "{:.1f}%",
            "ROAS": "{:.2f}",
            "CTR": "{:.2f}%",
            "FBA Available": "{:,.0f}",
            "Days of Supply": "{:,.0f}",
            "Sales Velocity": "{:,.1f}",
            "Orders": "{:,.0f}",
            "Units": "{:,.0f}",
        }
    )
    if "CM3%" in df_.columns:
        styled = styled.map(
            lambda v: "background-color: #F8CBAD" if pd.notna(v) and v < target_cm3
            else ("background-color: #C6EFCE" if pd.notna(v) else ""),
            subset=["CM3%"],
        )
    if "Days of Supply" in df_.columns:
        styled = styled.map(
            lambda v: "background-color: #F8CBAD" if pd.notna(v) and v < min_dos else "",
            subset=["Days of Supply"],
        )
    return styled


st.dataframe(_style(table), use_container_width=True, hide_index=True, height=600)

st.download_button(
    "Download filtered rows (CSV)",
    data=table.to_csv(index=False).encode("utf-8"),
    file_name=f"margin_{marketplace}_{pd.Timestamp(period):%Y%m%d}.csv",
    mime="text/csv",
)
