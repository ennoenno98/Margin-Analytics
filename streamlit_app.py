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


def latest_products_export(exports_dir: Path) -> Path | None:
    if not exports_dir.exists():
        return None
    candidates = sorted(
        list(exports_dir.glob("products_export_*.csv"))
        + list(exports_dir.glob("products_export_*.csv.gz"))
    )
    return candidates[-1] if candidates else None


@st.cache_data(show_spinner=False)
def load_products(path: Path) -> pd.DataFrame:
    """Latest-snapshot lookup of FBA inventory from the daily products feed.

    Returns one row per (SKU, Marketplace Name) — the most recent non-null
    values across the file's period range. Columns: FBA Available, FBA
    Incoming, AFN Reserved Quantity, FBA Total Inventory, Sales Velocity,
    Units Sold (30d), Days of Supply.
    """
    # Read only what we need — the products feed has 22 cols × 113k rows,
    # most of them text we never use (Brand, Store Name, filter columns).
    KEEP = {
        "Period", "SKU", "Marketplace Name",
        "FBA Available", "FBA Incoming", "AFN Reserved Quantity",
        "FBA Total Inventory", "Sales Velocity", "Units Sold (30d)",
        "Days of Supply",
    }
    p = pd.read_csv(path, usecols=lambda c: c in KEEP)
    p["Period"] = pd.to_datetime(p["Period"], errors="coerce", utc=True).dt.tz_localize(None)
    for col in ("SKU", "Marketplace Name"):
        if col in p.columns:
            try:
                p[col] = p[col].astype("string[pyarrow]")
            except (ImportError, TypeError):
                pass
    inventory_cols = [
        "FBA Available", "FBA Incoming", "AFN Reserved Quantity",
        "FBA Total Inventory", "Sales Velocity", "Units Sold (30d)",
        "Days of Supply",
    ]
    for c in inventory_cols:
        if c in p.columns:
            p[c] = pd.to_numeric(p[c], errors="coerce", downcast="float")
    # Take the latest non-null value per (SKU, marketplace) per column.
    p = p.sort_values("Period")
    keys = ["SKU", "Marketplace Name"]
    present_cols = [c for c in inventory_cols if c in p.columns]
    for c in present_cols:
        p[c] = p.groupby(keys)[c].ffill()
    latest = p.groupby(keys, as_index=False)[present_cols].last()

    # Derive missing columns when Novadata stops emitting them:
    # - Days of Supply = FBA Available / Sales Velocity (units/day).
    # - FBA Total Inventory ≈ Available + Incoming + Reserved.
    if ("Days of Supply" not in latest.columns
            and "FBA Available" in latest.columns
            and "Sales Velocity" in latest.columns):
        vel = pd.to_numeric(latest["Sales Velocity"], errors="coerce")
        latest["Days of Supply"] = (
            pd.to_numeric(latest["FBA Available"], errors="coerce")
            / vel.where(vel > 0)
        )
    if "FBA Total Inventory" not in latest.columns:
        parts = [pd.to_numeric(latest[c], errors="coerce").fillna(0)
                 for c in ("FBA Available", "FBA Incoming", "AFN Reserved Quantity")
                 if c in latest.columns]
        if parts:
            latest["FBA Total Inventory"] = sum(parts)
    return latest


@st.cache_data(show_spinner=False)
def load_data(path: Path) -> pd.DataFrame:
    """Load the daily margin export, dropping unused columns and shrinking
    dtypes to keep memory low (Streamlit Cloud's free tier caps at ~1 GB).

    The raw file is ~50 MB CSV / 174k rows × 26 cols; loaded naively that's
    ~500 MB in pandas (object dtypes on long product titles + unused IDs).
    After this function, the dataframe is closer to 50–80 MB.
    """
    # Whitelist: columns the dashboard actually uses. Anything not in here
    # (Seller Partner ID, Store Name, Marketplace, Parent ASIN, Brand) is
    # dropped at parse time so it never enters memory.
    KEEP = {
        "Period", "SKU", "Product", "Marketplace Name", "Child ASIN",
        "Orders", "Units", "Product Sales",
        "Contribution Margin 1", "Contribution Margin 2", "Contribution Margin 3",
        "Advertising Costs",
        "CM1%", "CM2%", "CM3%", "Sponsored Spend",  # legacy weekly schema
        "ROAS", "CTR",
        "FBA Available", "Days of Supply", "Sales Velocity",
        "UK filters", "FR filters", "ES filters", "DE filters", "IT filters",
    }
    df = pd.read_csv(path, usecols=lambda c: c in KEEP)
    df["Period"] = pd.to_datetime(df["Period"], errors="coerce", utc=True).dt.tz_localize(None)

    cm_abs = {
        "Contribution Margin 1": "CM1",
        "Contribution Margin 2": "CM2",
        "Contribution Margin 3": "CM3",
    }
    for src, short in cm_abs.items():
        if src in df.columns:
            df[short] = pd.to_numeric(df[src], errors="coerce", downcast="float")
    if "Advertising Costs" in df.columns:
        df["Sponsored Spend"] = pd.to_numeric(df["Advertising Costs"], errors="coerce", downcast="float")

    numeric_cols = [
        "CM1%", "CM2%", "CM3%", "Sponsored Spend", "ROAS", "CTR",
        "Product Sales", "FBA Available", "Days of Supply", "Sales Velocity",
        "CM1", "CM2", "CM3",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce", downcast="float")
    for col in ("Orders", "Units"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce", downcast="integer")

    # Derive CM%s when only absolute margins are present (daily schema).
    sales = df.get("Product Sales")
    if sales is not None:
        safe_sales = sales.where(sales > 0)
        for short in ["CM1", "CM2", "CM3"]:
            pct_col = f"{short}%"
            if short in df.columns and pct_col not in df.columns:
                df[pct_col] = (df[short] / safe_sales * 100).astype("float32")

    if "Days of Supply" not in df.columns and "FBA Available" in df.columns and "Sales Velocity" in df.columns:
        vel = df["Sales Velocity"]
        df["Days of Supply"] = (df["FBA Available"] / vel.where(vel > 0)).astype("float32")

    # Drop the now-redundant source columns (we keep the derived shorts).
    drop = [c for c in ("Contribution Margin 1", "Contribution Margin 2",
                        "Contribution Margin 3", "Advertising Costs")
            if c in df.columns]
    if drop:
        df = df.drop(columns=drop)

    # Override Product titles with the English version from
    # product_titles_en.json (anything not listed keeps the original Novadata
    # title). Applied BEFORE category conversion so the assignment doesn't
    # force a dtype change.
    titles_en = load_product_titles_en()
    if titles_en and "Product" in df.columns:
        en_mapped = df["SKU"].map(titles_en)
        mask = en_mapped.notna()
        if mask.any():
            df["Product"] = df["Product"].astype("object")  # release category if any
            df.loc[mask, "Product"] = en_mapped[mask]

    # Categorize low-cardinality strings (only the ones we never groupby on)
    # and use pyarrow-backed StringDtype for the rest (SKU, Marketplace Name).
    # Together this cuts the dataframe from ~500 MB → ~40 MB on the real data.
    for col in ("UK filters", "FR filters", "ES filters", "DE filters", "IT filters",
                "Child ASIN", "Product"):
        if col in df.columns:
            df[col] = df[col].astype("category")
    for col in ("SKU", "Marketplace Name"):
        if col in df.columns:
            try:
                df[col] = df[col].astype("string[pyarrow]")
            except (ImportError, TypeError):
                pass

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
ADJUSTMENTS_PATH = REPO_ROOT / "adjustments.json"
PRODUCT_TITLES_EN_PATH = REPO_ROOT / "product_titles_en.json"


@st.cache_data(show_spinner=False)
def load_product_titles_en() -> dict[str, str]:
    """SKU → English product title overrides loaded from product_titles_en.json.

    Novadata returns one Product title per SKU (in the seller's source
    language), so we override at load_data time when an English title is
    available. Anything not listed keeps its original Novadata title.
    """
    if not PRODUCT_TITLES_EN_PATH.exists():
        return {}
    try:
        raw = json.loads(PRODUCT_TITLES_EN_PATH.read_text())
    except Exception:
        return {}
    titles = raw.get("titles") if isinstance(raw, dict) else raw
    if not isinstance(titles, dict):
        return {}
    return {str(k): str(v) for k, v in titles.items() if v and str(v).strip()}


# ---------- Manual one-off adjustments ----------
# Read from adjustments.json. Each entry pro-rates a CM1/CM2/CM3 € delta over
# the days where the user's period selection overlaps the adjustment's
# date range. Applied AFTER aggregation, never written back to the Novadata
# data files. Used to back out abnormal events (massive returns, fraud,
# one-off chargebacks, etc.) without polluting the source dataset.
@st.cache_data(show_spinner=False)
def load_adjustments() -> dict[str, list[dict]]:
    if not ADJUSTMENTS_PATH.exists():
        return {}
    try:
        raw = json.loads(ADJUSTMENTS_PATH.read_text())
    except Exception:
        return {}
    out = {}
    for sku, entries in raw.items():
        if sku.startswith("_"):
            continue
        if isinstance(entries, dict):
            entries = [entries]
        out[sku] = entries
    return out


def apply_adjustments(filtered: pd.DataFrame, adjustments: dict, selected_periods: list):
    """Add pro-rated deltas, recompute %s, return (df, {SKU: note})."""
    if not adjustments or filtered.empty or not selected_periods:
        return filtered, {}
    sel = pd.to_datetime([pd.Timestamp(p) for p in selected_periods]).normalize()
    sel_set = set(sel)
    notes = {}
    f = filtered.copy()
    for sku, entries in adjustments.items():
        if sku not in set(f["SKU"]):
            continue
        for adj in entries:
            days = pd.date_range(adj["from"], adj["to"], freq="D").normalize()
            overlap = len(set(days) & sel_set)
            total = len(days)
            if not total or not overlap:
                continue
            ratio = overlap / total
            mask = f["SKU"] == sku
            for col, delta in (adj.get("deltas") or {}).items():
                if col in f.columns:
                    f.loc[mask, col] = (
                        pd.to_numeric(f.loc[mask, col], errors="coerce").fillna(0) + delta * ratio
                    )
            sales = pd.to_numeric(f.loc[mask, "Product Sales"], errors="coerce").fillna(0)
            for short in ("CM1", "CM2", "CM3"):
                if short in f.columns and f"{short}%" in f.columns:
                    f.loc[mask, f"{short}%"] = (
                        pd.to_numeric(f.loc[mask, short], errors="coerce")
                        / sales.where(sales > 0) * 100
                    )
            tag = "" if abs(ratio - 1) < 0.01 else f" (pro-rated × {ratio:.0%})"
            notes[sku] = (f"✱ ADJUSTED{tag}: {adj.get('note', 'Manual adjustment applied.')}"
                          + (f"  [{adj.get('applied_at')}]" if adj.get("applied_at") else ""))
    return f, notes


# ---------- Comments persistence ----------
# Streamlit Cloud's filesystem is ephemeral (resets on every code push or idle
# reboot). To keep comments across container lifetimes we mirror them to a
# private GitHub Gist when GITHUB_TOKEN + COMMENTS_GIST_ID are set in
# st.secrets. The local file stays as a fallback.

GIST_FILE = "comments.json"


def _gist_config():
    """(gist_id, token) tuple, or (None, None) if not configured."""
    try:
        token = st.secrets.get("GITHUB_TOKEN")
        gist_id = st.secrets.get("COMMENTS_GIST_ID")
    except Exception:
        token = os.environ.get("GITHUB_TOKEN")
        gist_id = os.environ.get("COMMENTS_GIST_ID")
    return (gist_id, token) if (token and gist_id) else (None, None)


def _load_comments_from_gist(gist_id: str, token: str):
    """Return (data, detail). data is None on failure; detail explains why."""
    import requests
    try:
        r = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except Exception as exc:
        return None, f"network error: {exc!s}"
    if r.status_code == 200:
        files = r.json().get("files", {})
        content = (files.get(GIST_FILE) or {}).get("content", "{}")
        try:
            return json.loads(content), "ok"
        except Exception:
            return {}, "Gist content isn't valid JSON — starting from {}"
    msg = {
        401: "401 — token rejected (invalid / expired)",
        403: "403 — token lacks Gists: read & write, or belongs to a different user",
        404: "404 — Gist ID not found by this token",
    }.get(r.status_code, f"HTTP {r.status_code}")
    try:
        body_msg = r.json().get("message")
        if body_msg:
            msg = f"{msg}: {body_msg}"
    except Exception:
        pass
    return None, msg


def _save_comments_to_gist(gist_id: str, token: str, comments: dict):
    """Return (ok: bool, detail: str). Detail explains the HTTP failure when
    it's not a 200 — so the UI status line can say WHY a write was rejected
    instead of just 'failed'."""
    import requests
    body = {"files": {GIST_FILE: {"content": json.dumps(comments, indent=2, ensure_ascii=False)}}}
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json=body,
            timeout=10,
        )
    except Exception as exc:
        return False, f"network error: {exc!s}"
    if r.status_code == 200:
        return True, "ok"
    # Common cases: 401 (bad token), 403 (token lacks Gists scope or wrong
    # account), 404 (gist id wrong / not visible to this token), 422 (body).
    msg = {
        401: "401 — token rejected (invalid / expired)",
        403: "403 — token doesn't have Gists: read & write, or doesn't own this Gist",
        404: "404 — Gist not found by this token (wrong ID, or PAT belongs to a different user)",
        422: "422 — Gist payload rejected",
    }.get(r.status_code, f"HTTP {r.status_code}")
    # Append the GitHub-reported message when available.
    try:
        body_msg = r.json().get("message")
        if body_msg:
            msg = f"{msg}: {body_msg}"
    except Exception:
        pass
    return False, msg


# Marker key used for legacy single-string comments migrated from the old
# flat-by-SKU schema. Rendered with a "[legacy]" prefix wherever shown.
LEGACY_KEY = "_legacy"
# Cross-country / global note slot, editable in the All-countries view.
GLOBAL_KEY = "_all"

FLAGS = {
    "amazon.de": "🇩🇪",
    "amazon.co.uk": "🇬🇧",
    "amazon.fr": "🇫🇷",
    "amazon.es": "🇪🇸",
    "amazon.it": "🇮🇹",
    "amazon.nl": "🇳🇱",
    "amazon.ie": "🇮🇪",
    "amazon.se": "🇸🇪",
    "amazon.com.be": "🇧🇪",
    "amazon.be": "🇧🇪",
    "amazon.pl": "🇵🇱",
}


def _normalise_comments(raw: dict) -> dict:
    """Migrate any flat {sku: str} entries to the nested per-country format
    {sku: {country: text}}. Idempotent — safe to run on already-normalised
    data.
    """
    out: dict[str, dict[str, str]] = {}
    for sku, val in (raw or {}).items():
        if not sku:
            continue
        if isinstance(val, dict):
            inner = {str(k): str(v) for k, v in val.items() if v and str(v).strip()}
            if inner:
                out[sku] = inner
        elif isinstance(val, str) and val.strip():
            out[sku] = {LEGACY_KEY: val.strip()}
    return out


def load_comments() -> dict:
    """Returns nested dict: {sku: {country: text}}.
    Country can be a marketplace name (e.g. 'amazon.de') or '_legacy'.
    Also writes the read result into st.session_state['comments_status'] so
    the UI can immediately surface a misconfigured Gist on page load (instead
    of waiting for the user to edit a cell).
    """
    gist_id, token = _gist_config()
    if gist_id and token:
        remote, detail = _load_comments_from_gist(gist_id, token)
        if remote is not None:
            st.session_state["comments_status"] = (
                "synced to Gist ✓" if detail == "ok" else f"loaded from Gist with warning: {detail}"
            )
            return _normalise_comments(remote)
        st.session_state["comments_status"] = (
            f"Gist read failed ({detail}) — falling back to session-only ⚠"
        )
    if not COMMENTS_PATH.exists():
        return {}
    try:
        return _normalise_comments(json.loads(COMMENTS_PATH.read_text()))
    except Exception:
        return {}


def _country_tag(marketplace: str) -> str:
    """Display tag with flag for a marketplace, e.g. 'amazon.de' → '🇩🇪 DE'."""
    if not marketplace or marketplace == LEGACY_KEY:
        return "*"
    flag = FLAGS.get(marketplace, "")
    code = marketplace.replace("amazon.", "").replace("co.uk", "uk").upper()
    return f"{flag} {code}".strip()


def global_note_for(sku: str, store: dict) -> str:
    """Return the cross-country global note for a SKU (the '_all' slot)."""
    return (store.get(sku) or {}).get(GLOBAL_KEY, "") or ""


def comment_for_view(sku: str, marketplace: str, store: dict) -> str:
    """Per-country Comments cell content for one SKU.

    - Single-country view: return that country's note. Legacy migrated notes
      fall through with a '[legacy] ' prefix when no country-specific note
      has been written yet.
    - All-countries view (marketplace == None or '__all__'): concatenate
      every per-country note, each prefixed by its flag tag (e.g.
      '🇩🇪 DE: …  ·  🇫🇷 FR: …'). The global note has its own column.
    """
    entry = store.get(sku) or {}
    if not entry:
        return ""
    if marketplace and marketplace != "__all__":
        own = entry.get(marketplace)
        if own:
            return own
        legacy = entry.get(LEGACY_KEY)
        return f"[legacy] {legacy}" if legacy else ""
    # All-countries: concatenate per-country notes, prefixed with flag tags.
    # The GLOBAL_KEY note lives in its own column, so skip it here.
    parts = []
    for mp, note in entry.items():
        if not note or mp == GLOBAL_KEY:
            continue
        if mp == LEGACY_KEY:
            parts.append(f"[legacy] {note}")
        else:
            parts.append(f"{_country_tag(mp)}: {note}")
    return "  ·  ".join(parts)


def _clean_text(value) -> str:
    """Strip display chrome we auto-inject into Comments cells so it isn't
    persisted back into storage."""
    text = (str(value) if value is not None else "")
    if "✱ ADJUSTED" in text and "|" in text:
        text = text.split("|", 1)[1]
    for prefix in ("[legacy] ", "🌍 "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip()


def persist_comment_edits(edited_skus, edited_comments=None, marketplace: str | None = None,
                          edited_global=None) -> bool:
    """Diff Comments / Global note columns against the store and persist.

    - ``edited_comments`` (per-country column) is saved against the active
      marketplace; passing None / '__all__' as marketplace makes this a no-op
      so the cross-country merged cell stays read-only.
    - ``edited_global`` (the GLOBAL_KEY '_all' slot) is editable in any view
      and saves regardless of marketplace.
    """
    store: dict[str, dict[str, str]] = dict(st.session_state.get("comments", {}))
    changed = False
    skus = list(edited_skus)
    country_iter = list(edited_comments) if edited_comments is not None else [None] * len(skus)
    global_iter = list(edited_global) if edited_global is not None else [None] * len(skus)

    country_writable = bool(marketplace) and marketplace != "__all__"

    for sku, country_text, global_text in zip(skus, country_iter, global_iter):
        entry = dict(store.get(sku, {}))
        sku_changed = False

        if country_writable and edited_comments is not None:
            text = _clean_text(country_text)
            prev = entry.get(marketplace, "")
            if text != prev:
                if text:
                    entry[marketplace] = text
                    entry.pop(LEGACY_KEY, None)
                else:
                    entry.pop(marketplace, None)
                sku_changed = True

        if edited_global is not None:
            text = _clean_text(global_text)
            prev = entry.get(GLOBAL_KEY, "")
            if text != prev:
                if text:
                    entry[GLOBAL_KEY] = text
                else:
                    entry.pop(GLOBAL_KEY, None)
                sku_changed = True

        if sku_changed:
            if entry:
                store[sku] = entry
            elif sku in store:
                del store[sku]
            changed = True

    if changed:
        st.session_state["comments"] = store
        try:
            st.session_state["comments_status"] = save_comments(store)
        except Exception as exc:
            st.session_state["comments_status"] = f"save failed: {exc}"
    return changed


def save_comments(comments: dict) -> str:
    """Return a short status string for UI display. Stores nested format."""
    clean = {
        sku: {k: v for k, v in (entry or {}).items() if v and str(v).strip()}
        for sku, entry in (comments or {}).items()
    }
    clean = {sku: inner for sku, inner in clean.items() if inner}
    try:
        COMMENTS_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    except Exception:
        pass
    gist_id, token = _gist_config()
    if gist_id and token:
        ok, detail = _save_comments_to_gist(gist_id, token, clean)
        if ok:
            return "synced to Gist ✓"
        return f"Gist write failed ({detail}) — session only ⚠"
    return "session only — configure GITHUB_TOKEN + COMMENTS_GIST_ID for persistence"


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
        # Force float dtype — division on float32 with where() can otherwise
        # propagate object dtype when all values are NaN, which trips AgGrid.
        agg[c] = (agg["num"] / agg["den"].where(agg["den"] > 0)).astype("float32")
        base = base.merge(agg[[c]], left_on="SKU", right_index=True, how="left")

    # When absolute margins are present (daily schema), prefer the cleaner
    # derivation: %CM = Σ(CM) / Σ(Sales) × 100. This overrides the weighted-avg
    # values computed above where possible.
    if "Product Sales" in base.columns:
        sales_total = pd.to_numeric(base["Product Sales"], errors="coerce")
        for short in ["CM1", "CM2", "CM3"]:
            if short in base.columns:
                cm_total = pd.to_numeric(base[short], errors="coerce")
                base[f"{short}%"] = (cm_total / sales_total.where(sales_total > 0) * 100).astype("float32")
    return base


def _cm3_pct_by_bucket(raw: pd.DataFrame, freq: str) -> pd.DataFrame:
    """CM3% per (SKU, time bucket) within a slice of raw period rows.

    `freq` is a pandas period alias ('D' or 'W'). CM3% is computed from
    totals (Σ CM3 / Σ Sales) when the absolute margin column is present,
    otherwise from a sales-weighted average of the CM3% column.
    Returns long-form columns: SKU, bucket, CM3%.
    """
    r = raw.dropna(subset=["Period"]).copy()
    if r.empty:
        return pd.DataFrame(columns=["SKU", "bucket", "CM3%"])
    r["bucket"] = r["Period"].dt.to_period(freq).dt.start_time
    r["Product Sales"] = pd.to_numeric(r.get("Product Sales"), errors="coerce")
    if "CM3" in r.columns:
        r["CM3"] = pd.to_numeric(r["CM3"], errors="coerce")
        grp = r.groupby(["SKU", "bucket"], as_index=False).agg(
            _cm3=("CM3", "sum"), _sales=("Product Sales", "sum")
        )
        grp["CM3%"] = grp["_cm3"] / grp["_sales"].where(grp["_sales"] > 0) * 100
    else:
        r["CM3%"] = pd.to_numeric(r.get("CM3%"), errors="coerce")
        r["_w"] = (r["CM3%"] * r["Product Sales"]).where(
            r["CM3%"].notna() & r["Product Sales"].notna()
        )
        grp = r.groupby(["SKU", "bucket"], as_index=False).agg(
            _num=("_w", "sum"), _sales=("Product Sales", "sum")
        )
        grp["CM3%"] = grp["_num"] / grp["_sales"].where(grp["_sales"] > 0) * 100
    grp = grp.rename(columns={"_sales": "Sales"})
    return grp[["SKU", "bucket", "CM3%", "Sales"]].dropna(subset=["CM3%"])


def margin_trend(raw: pd.DataFrame, freq: str,
                 threshold_pp: float = 2.0, min_buckets: int = 2) -> pd.DataFrame:
    """Per-SKU margin trend over the selected window.

    Fits a straight line to each SKU's CM3% across the time buckets and
    classifies the predicted change from first to last bucket:
      Rising    → change ≥ +threshold_pp
      Declining → change ≤ −threshold_pp
      Neutral   → in between
    SKUs with fewer than `min_buckets` data points are excluded.

    Returns columns: SKU, Trend, Slope (pp/bucket), Change (pp),
    Start CM3%, End CM3%, Points.
    """
    import numpy as np

    long = _cm3_pct_by_bucket(raw, freq)
    if long.empty:
        return pd.DataFrame(
            columns=["SKU", "Trend", "Slope", "Change",
                     "Start CM3%", "End CM3%", "Start period", "End period", "Points"]
        )

    rows = []
    for sku, g in long.groupby("SKU"):
        g = g.sort_values("bucket")
        n = len(g)
        if n < min_buckets:
            continue
        x = np.arange(n, dtype=float)
        y = g["CM3%"].to_numpy(dtype=float)
        # Weight the fit by sales so low-volume weeks (which can show wild CM3%
        # from a tiny denominator) don't dominate the slope. Fall back to an
        # unweighted fit if weights are missing or all zero.
        w = pd.to_numeric(g.get("Sales"), errors="coerce").to_numpy(dtype=float)
        if w is None or not np.isfinite(w).any() or np.nansum(w) <= 0:
            slope, intercept = np.polyfit(x, y, 1)
        else:
            w = np.where(np.isfinite(w) & (w > 0), w, 0.0)
            slope, intercept = np.polyfit(x, y, 1, w=np.sqrt(w))
        change = slope * (n - 1)            # predicted first→last change
        if change >= threshold_pp:
            trend = "Rising"
        elif change <= -threshold_pp:
            trend = "Declining"
        else:
            trend = "Neutral"
        buckets = g["bucket"].tolist()
        rows.append({
            "SKU": sku,
            "Trend": trend,
            "Slope": slope,
            "Change": change,
            "Start CM3%": y[0],
            "End CM3%": y[-1],
            "Start period": buckets[0],
            "End period": buckets[-1],
            "Points": n,
        })
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def monthly_sales_all_countries(df_full: pd.DataFrame) -> pd.Series:
    """Per-SKU revenue over the trailing 30 days, summed across ALL marketplaces.

    Used as a stable 'product size' gate independent of the marketplace and
    period currently being viewed. Indexed by SKU.
    """
    d = df_full.dropna(subset=["Period"]).copy()
    if d.empty:
        return pd.Series(dtype="float64")
    latest = d["Period"].max()
    window = d[d["Period"] > latest - pd.Timedelta(days=30)]
    sales = pd.to_numeric(window["Product Sales"], errors="coerce")
    return sales.groupby(window["SKU"]).sum()


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
# ----- Conditional-format thresholds -----
# Edit these in code if you need different breakpoints; the previous sidebar
# controls were rarely changed and added visual noise.
target_cm3 = 19.7
min_dos = 30

# ----- Top filters (shared by Overview + Compare) -----
with st.container(border=True):
    c1, c2, c3, c4, c5 = st.columns([1.2, 0.9, 1.6, 1.8, 1])
    real_marketplaces = sorted(df["Marketplace Name"].dropna().unique().tolist())
    ALL_OPTION = "🌍 All countries"
    marketplaces = [ALL_OPTION] + real_marketplaces
    marketplace = c1.selectbox("Marketplace", marketplaces, index=0)  # default: All countries
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

    # Revenue slicer — gate on each SKU's trailing-30-day sales across ALL
    # countries, so the threshold is a stable "product size" cut regardless of
    # the marketplace / period currently selected. Default €2.5k.
    sku_monthly_rev = monthly_sales_all_countries(df)
    rev_max = float(sku_monthly_rev.max()) if len(sku_monthly_rev) else 0.0
    r1, r2 = st.columns([1, 3])
    min_monthly_sales = r1.number_input(
        "Min monthly sales (€, all countries)",
        min_value=0, value=2500, step=500,
        help="Hide SKUs whose combined sales across all marketplaces over the "
             "last 30 days are below this. Set to 0 to show everything.",
    )
    if rev_max:
        n_pass = int((sku_monthly_rev >= min_monthly_sales).sum())
        r2.caption(
            f"{n_pass:,} of {len(sku_monthly_rev):,} SKUs clear "
            f"€{min_monthly_sales:,.0f}/mo (all countries). "
            f"Trailing 30 days; highest is €{rev_max:,.0f}."
        )

# Revenue gate (all-country trailing-30-day sales per SKU). Applied at the
# slice level so EVERY downstream view — table, cluster matrix, per-country
# breakdown, margin trend — inherits the same SKU universe.
if min_monthly_sales > 0 and len(sku_monthly_rev):
    passing_skus = set(sku_monthly_rev[sku_monthly_rev >= min_monthly_sales].index)
else:
    passing_skus = None

# Marketplace base slice (all weeks for this marketplace). Boolean-index views
# only — no .copy() on the cached load_data frame, which is up to ~80 MB.
if is_all_countries:
    mp_slice = df
else:
    mp_slice = df[df["Marketplace Name"] == marketplace]
if passing_skus is not None:
    mp_slice = mp_slice[mp_slice["SKU"].isin(passing_skus)]

# Current view: aggregate across the selected weeks (sum + weighted avg).
raw_slice = mp_slice[mp_slice["Period"].isin(selected_periods)]
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

tab_overview, tab_trend, tab_slow = st.tabs(["Overview", "Margin Trend", "Slow movers"])

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

    # ----- Inventory columns: always use the LATEST snapshot's values -----
    # Two data sources:
    #   - Margin feed (always available): FBA Available, Days of Supply,
    #     Sales Velocity — pulled from the latest daily row per SKU.
    #   - Products feed (if present): adds FBA Incoming, AFN Reserved
    #     Quantity, FBA Total Inventory, Units Sold (30d). Falls through
    #     cleanly if the file is missing or fields are all NaN.
    latest_mp_period = max(mp_slice["Period"].dropna().unique())
    latest_rows = mp_slice[mp_slice["Period"] == latest_mp_period].copy()
    margin_inv_cols = [c for c in ["FBA Available", "Days of Supply", "Sales Velocity"]
                       if c in latest_rows.columns]
    for col in margin_inv_cols:
        latest_rows[col] = pd.to_numeric(latest_rows[col], errors="coerce")

    inventory_latest = pd.DataFrame(index=latest_rows["SKU"].drop_duplicates())
    # Additive across marketplaces in 'All countries' mode.
    if "FBA Available" in margin_inv_cols:
        inventory_latest["FBA Available"] = latest_rows.groupby("SKU")["FBA Available"].sum(min_count=1)
    if "Sales Velocity" in margin_inv_cols:
        inventory_latest["Sales Velocity"] = latest_rows.groupby("SKU")["Sales Velocity"].sum(min_count=1)
    if "Days of Supply" in margin_inv_cols:
        dos_v = pd.to_numeric(latest_rows["Days of Supply"], errors="coerce")
        weights = pd.to_numeric(latest_rows.get("FBA Available"), errors="coerce")
        if weights is None or weights.isna().all():
            inventory_latest["Days of Supply"] = latest_rows.groupby("SKU")["Days of Supply"].mean()
        else:
            valid = dos_v.notna() & weights.notna() & (weights > 0)
            tmp = pd.DataFrame({
                "SKU": latest_rows["SKU"],
                "_v": (dos_v * weights).where(valid),
                "_w": weights.where(valid),
            })
            dos_agg = tmp.groupby("SKU").agg(num=("_v", "sum"), den=("_w", "sum"))
            inventory_latest["Days of Supply"] = dos_agg["num"] / dos_agg["den"].where(dos_agg["den"] > 0)

    # Augment with the products feed. It now populates the core inventory
    # columns (which the margin feed leaves empty), so we OVERRIDE on non-null
    # values from products and only fall back to the margin feed otherwise.
    products_cols = ["FBA Available", "FBA Incoming", "AFN Reserved Quantity",
                     "FBA Total Inventory", "Sales Velocity", "Days of Supply",
                     "Units Sold (30d)"]
    products_path = latest_products_export(EXPORTS_DIR)
    products_caption = ""
    if products_path is not None:
        products_lookup = load_products(products_path)
        # Filter to the marketplace(s) in view.
        if is_all_countries:
            p_rows = products_lookup
        else:
            p_rows = products_lookup[products_lookup["Marketplace Name"] == marketplace]
        for col in products_cols:
            if col not in p_rows.columns:
                continue
            # Sum across marketplaces in All-countries mode (additive units);
            # for ratios like Days of Supply we recompute from totals below.
            if col == "Days of Supply":
                continue
            inventory_latest[col] = p_rows.groupby("SKU")[col].sum(min_count=1)
        # Recompute Days of Supply from the (possibly aggregated) totals.
        if ("FBA Available" in inventory_latest.columns
                and "Sales Velocity" in inventory_latest.columns):
            vel = pd.to_numeric(inventory_latest["Sales Velocity"], errors="coerce")
            inventory_latest["Days of Supply"] = (
                pd.to_numeric(inventory_latest["FBA Available"], errors="coerce")
                / vel.where(vel > 0)
            )
        has_any = any(
            col in p_rows.columns and p_rows[col].notna().any()
            for col in products_cols
        )
        if not has_any:
            products_caption = (
                f" Products feed `{products_path.name}` has no inventory values yet — "
                f"will populate once Novadata starts emitting those fields."
            )

    for col in inventory_latest.columns:
        filtered[col] = filtered["SKU"].map(inventory_latest[col])
    inventory_caption = (
        f"Inventory columns always show the latest snapshot "
        f"({_fmt_day(latest_mp_period)}), regardless of the selected period."
        + products_caption
    )

    # ----- Revenue growth: same calendar weeks one month earlier (shift back 4) -----
    prior_4w_set = _equivalent_prior_set(selected_periods, 28)
    if prior_4w_set:
        cur_rev = filtered.set_index("SKU")["Product Sales"]
        prior_4w_raw = mp_slice[mp_slice["Period"].isin(prior_4w_set)]
        prior_4w_agg = aggregate_periods(prior_4w_raw)
        prev_rev = prior_4w_agg.set_index("SKU")["Product Sales"]
        # Align by SKU; some current SKUs may not appear in the prior set.
        prev_rev_aligned = prev_rev.reindex(cur_rev.index)
        wow4 = ((cur_rev - prev_rev_aligned)
                / prev_rev_aligned.where(prev_rev_aligned > 0)) * 100
        filtered["Rev Δ 4w %"] = filtered["SKU"].map(wow4)
        growth_caption = (
            f"Rev Δ 4w % compares the selection to the same days 4 weeks earlier "
            f"(ending {_fmt_day(max(prior_4w_set))})."
        )
    else:
        filtered["Rev Δ 4w %"] = pd.NA
        growth_caption = "No equivalent set 4 weeks back, so Rev Δ 4w % is empty."

    # ----- Apply manual one-off adjustments (adjustments.json) -----
    adjustments = load_adjustments()
    filtered, adj_notes = apply_adjustments(filtered, adjustments, selected_periods)

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
            if passing_skus is not None:
                mp_rows = mp_rows[mp_rows["SKU"].isin(passing_skus)]
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
                width="stretch",
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
            st.plotly_chart(country_fig, width="stretch")

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
                        width="stretch",
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

    toggle_col1, toggle_col2 = st.columns(2)
    below_target_only = toggle_col1.checkbox(
        f"Show only SKUs with CM3% below {target_cm3:.0f}%", value=False
    )
    show_inventory = toggle_col2.toggle(
        "Show inventory columns",
        value=True,
        help="Hide FBA Available / Incoming / Reserved / Total, Days of Supply, "
             "Sales Velocity and Units (30d) to focus on margin & sales.",
    )
    if below_target_only:
        filtered = filtered[filtered["CM3%"] < target_cm3]

    # ----- Per-SKU comments (loaded from comments.json) -----
    # Comments are stored per (SKU, country). In a single-country view the
    # cell shows / edits that country's note. In All-countries view it shows
    # every country's note concatenated, read-only.
    if "comments" not in st.session_state:
        st.session_state["comments"] = load_comments()
    comment_scope = "__all__" if is_all_countries else marketplace
    filtered["Comments"] = [
        comment_for_view(sku, comment_scope, st.session_state["comments"])
        for sku in filtered["SKU"]
    ]
    # Cross-country / global note (always per-SKU, editable in All countries
    # mode, shown in country views for context).
    filtered["Global note"] = [
        global_note_for(sku, st.session_state["comments"])
        for sku in filtered["SKU"]
    ]
    # Surface manual-adjustment notes from adjustments.json so adjusted rows
    # are visibly marked in the table.
    if adj_notes:
        def _merge_note(row):
            adj = adj_notes.get(row["SKU"], "")
            user = row.get("Comments") or ""
            if adj and user:
                return f"{adj} | {user}"
            return adj or user
        filtered["Comments"] = filtered.apply(_merge_note, axis=1)
        st.info(
            f"**Manual adjustments applied to {len(adj_notes)} SKU(s).** "
            f"Affected rows show a ✱ ADJUSTED marker in Comments. "
            f"Source: `adjustments.json` (one-off, not in Novadata)."
        )

    INVENTORY_COLS = {
        "FBA Available", "FBA Incoming", "AFN Reserved Quantity",
        "FBA Total Inventory", "Days of Supply", "Sales Velocity",
        "Units Sold (30d)",
    }

    display_cols = [
        "SKU", "Product", "Global note", "Comments", "Cluster",
        "Units", "Product Sales", "Rev Δ 4w %",
        "CM1%", "CM2%", "CM3%", "Δ CM3 vs prior", "P&L Impact",
        "Sponsored Spend",
        "FBA Available", "FBA Incoming", "AFN Reserved Quantity",
        "FBA Total Inventory", "Days of Supply", "Sales Velocity",
        "Units Sold (30d)",
        "Child ASIN",
    ]
    display_cols = [c for c in display_cols if c in filtered.columns]
    if not show_inventory:
        display_cols = [c for c in display_cols if c not in INVENTORY_COLS]
    table = filtered[display_cols].sort_values(
        "Product Sales", ascending=False, na_position="last"
    )

    st.caption(delta_caption + " · " + growth_caption + " · " + inventory_caption)

    # --- Unified editable table with conditional cell formatting via AgGrid ---
    from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

    fmt_euro = JsCode("function(p){return p.value==null?'—':'€'+Number(p.value).toLocaleString('de-DE',{maximumFractionDigits:0});}")
    fmt_int = JsCode("function(p){return p.value==null?'—':Number(p.value).toLocaleString('de-DE',{maximumFractionDigits:0});}")
    fmt_pct = JsCode("function(p){return p.value==null?'—':Number(p.value).toFixed(1)+'%';}")
    fmt_pct_signed = JsCode("function(p){if(p.value==null)return '—';var s=p.value>=0?'+':'';return s+Number(p.value).toFixed(1)+'%';}")
    fmt_pp = JsCode("function(p){if(p.value==null)return '—';var s=p.value>=0?'+':'';return s+Number(p.value).toFixed(1)+' pp';}")
    fmt_float2 = JsCode("function(p){return p.value==null?'—':Number(p.value).toFixed(0);}")
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
    # Defensive: AgGrid serialises the dataframe to the front end and has had
    # issues with pandas extension dtypes (pyarrow-string, category, nullable
    # Int64). Convert these to plain object/float at the render boundary so
    # the JS layer always sees standard JSON types. Memory-optimised dtypes
    # are preserved everywhere else.
    for _col in list(table_for_grid.columns):
        _dt = str(table_for_grid[_col].dtype)
        if _dt in ("string", "category"):
            table_for_grid[_col] = table_for_grid[_col].astype("object")
        elif _dt in ("Int64", "Int32", "Int16", "Int8"):
            table_for_grid[_col] = pd.to_numeric(table_for_grid[_col], errors="coerce").astype("float64")
    # Keep the full product title in a hidden column so the AgGrid tooltip can
    # show it, and replace the visible value with a short version: text before
    # the first separator, capped at 50 chars.
    if "Product" in table_for_grid.columns:
        def _short_product(name):
            if not isinstance(name, str) or not name:
                return name
            short = name
            for sep in (" | ", " — ", " - ", ", "):
                if sep in short:
                    short = short.split(sep, 1)[0]
                    break
            return short if len(short) <= 50 else short[:47] + "…"
        table_for_grid["Product Full"] = table_for_grid["Product"]
        table_for_grid["Product"] = table_for_grid["Product"].apply(_short_product)
    if "Cluster Code" in filtered.columns and "Cluster Code" not in table_for_grid.columns:
        table_for_grid["Cluster Code"] = filtered.set_index("SKU")["Cluster Code"].reindex(
            table_for_grid["SKU"].values
        ).values

    gb = GridOptionsBuilder.from_dataframe(table_for_grid)
    base_cell_style = {
        "color": "#111",
        "fontSize": "14px",
        "lineHeight": "40px",
        "paddingLeft": "10px",
        "paddingRight": "10px",
    }
    gb.configure_default_column(
        editable=False, resizable=True, sortable=True, filter=True,
        cellStyle=base_cell_style,
        wrapHeaderText=False,
        headerClass="big-header",
        minWidth=120,
        suppressSizeToFit=False,
    )

    def _style(extra):
        merged = dict(base_cell_style)
        if isinstance(extra, dict):
            merged.update(extra)
        return merged

    # All conditional styles need the base padding/font merged in, plus their colors.
    style_cm3_full = JsCode(
        f"function(p){{var b={{color:'#111',fontSize:'14px',lineHeight:'40px',paddingLeft:'10px',paddingRight:'10px'}};"
        f"if(p.value==null)return b;"
        f"if(p.value<{target_cm3}){{b.backgroundColor='#F8CBAD';return b;}}"
        f"b.backgroundColor='#C6EFCE';return b;}}"
    )
    style_delta_text_full = JsCode(
        "function(p){var b={color:'#111',fontSize:'14px',lineHeight:'40px',paddingLeft:'10px',paddingRight:'10px'};"
        "if(p.value==null)return b;"
        "if(p.value<0){b.color='#B71C1C';b.fontWeight='600';return b;}"
        "if(p.value>0){b.color='#1B5E20';b.fontWeight='600';return b;}return b;}"
    )
    style_dos_full = JsCode(
        f"function(p){{var b={{color:'#111',fontSize:'14px',lineHeight:'40px',paddingLeft:'10px',paddingRight:'10px'}};"
        f"if(p.value==null)return b;if(p.value<{min_dos}){{b.backgroundColor='#F8CBAD';return b;}}return b;}}"
    )
    style_cluster_full = JsCode(
        "function(p){var b={color:'#111',fontSize:'14px',lineHeight:'40px',paddingLeft:'10px',paddingRight:'10px'};"
        "var c=p.data && p.data['Cluster Code'];"
        "if(!c||c.indexOf('NA')>-1)return b;"
        "var parts=c.split('-');var m=parts[0],v=parts[1];"
        "if(m=='1'&&v=='1'){b.backgroundColor='#C6EFCE';b.fontWeight='600';return b;}"
        "if(m=='3'&&v=='3'){b.backgroundColor='#F8CBAD';return b;}"
        "if(m=='1'){b.backgroundColor='#E2F0D9';return b;}"
        "if(v=='1'){b.backgroundColor='#DEEBF7';return b;}return b;}"
    )

    # Per-column formatting + conditional styling. Wider defaults: text columns
    # Default widths sized so the header + a typical value fit without
    # the user having to drag-resize the column.
    column_specs = {
        "SKU": dict(width=160, pinned="left"),
        "Product": dict(width=300, pinned="left",
                        tooltipValueGetter=JsCode(
                            "function(p){return (p.data && p.data['Product Full']) || p.value;}"
                        )),
        "Product Full": dict(hide=True),
        "Comments": dict(
            editable=not is_all_countries, width=280,
            headerName=(
                f"Comments — all countries (read-only)" if is_all_countries
                else f"Comments — {_country_tag(marketplace)} ✏"
            ),
            cellStyle=_style({"backgroundColor": "#FFFDE7"}),
            wrapText=True, autoHeight=True,
        ),
        "Global note": dict(
            editable=True, width=240,
            headerName="🌍 Global note ✏",
            cellStyle=_style({"backgroundColor": "#E8F0FF"}),
            wrapText=True, autoHeight=True,
        ),
        "Cluster": dict(width=230, cellStyle=style_cluster_full),
        "Cluster Code": dict(hide=True),
        "Margin Tier": dict(hide=True),
        "Volume Tier": dict(hide=True),
        "Units": dict(width=130, type=["numericColumn"], valueFormatter=fmt_int),
        "Product Sales": dict(width=170, headerName="Sales (€)", type=["numericColumn"], valueFormatter=fmt_euro),
        "Rev Δ 4w %": dict(width=160, headerName="Rev Δ 4w", type=["numericColumn"], valueFormatter=fmt_pct_signed, cellStyle=style_delta_text_full),
        "CM1%": dict(width=120, type=["numericColumn"], valueFormatter=fmt_pct),
        "CM2%": dict(width=120, type=["numericColumn"], valueFormatter=fmt_pct),
        "CM3%": dict(width=120, type=["numericColumn"], valueFormatter=fmt_pct, cellStyle=style_cm3_full),
        "Δ CM3 vs prior": dict(width=170, headerName="Δ CM3", type=["numericColumn"], valueFormatter=fmt_pp, cellStyle=style_delta_text_full),
        "P&L Impact": dict(width=170, headerName="P&L (€)", type=["numericColumn"], valueFormatter=fmt_euro),
        "Sponsored Spend": dict(width=170, headerName="Ad spend (€)", type=["numericColumn"], valueFormatter=fmt_euro),
        "FBA Available": dict(width=150, headerName="FBA Avail.", type=["numericColumn"], valueFormatter=fmt_int),
        "FBA Incoming": dict(width=150, headerName="FBA Incoming", type=["numericColumn"], valueFormatter=fmt_int),
        "AFN Reserved Quantity": dict(width=160, type=["numericColumn"], valueFormatter=fmt_int, headerName="AFN Reserved"),
        "FBA Total Inventory": dict(width=150, type=["numericColumn"], valueFormatter=fmt_int, headerName="FBA Total"),
        "Days of Supply": dict(width=160, headerName="Days of Supply", type=["numericColumn"], valueFormatter=fmt_int, cellStyle=style_dos_full),
        "Sales Velocity": dict(width=150, headerName="Velocity / d", type=["numericColumn"], valueFormatter=fmt_float1),
        "Units Sold (30d)": dict(width=150, type=["numericColumn"], valueFormatter=fmt_int, headerName="Units (30d)"),
        "Child ASIN": dict(width=150),
    }
    for col, spec in column_specs.items():
        if col in table_for_grid.columns:
            gb.configure_column(col, **spec)

    grid_options = gb.build()
    grid_options["rowHeight"] = 40
    grid_options["headerHeight"] = 48
    # Force a normal layout so the body scrolls instead of letting the grid
    # grow tall and bleed into the page scroll.
    grid_options["domLayout"] = "normal"
    grid_options["alwaysShowHorizontalScroll"] = True
    grid_options["suppressHorizontalScroll"] = False
    # Slightly larger header text so the column names don't visually collide.
    st.markdown(
        """<style>
        .ag-theme-streamlit .ag-header-cell-label { font-weight: 600 !important; font-size: 13px !important; }
        .ag-theme-streamlit .ag-cell { font-size: 14px !important; }
        </style>""",
        unsafe_allow_html=True,
    )
    grid_response = AgGrid(
        table_for_grid,
        gridOptions=grid_options,
        allow_unsafe_jscode=True,
        update_on=["cellValueChanged"],
        fit_columns_on_grid_load=False,
        height=640,
        theme="streamlit",
        key=f"main_grid_{marketplace}_{period}_{granularity}",
    )

    edited = pd.DataFrame(grid_response["data"]) if grid_response and "data" in grid_response else table_for_grid

    # Persist any comment changes back to comments.json / session.
    # All-countries view is read-only (the cell is a concatenation of every
    # country's note); persist_comment_edits will no-op in that case.
    if "SKU" in edited.columns:
        persist_comment_edits(
            edited["SKU"],
            edited_comments=edited["Comments"] if "Comments" in edited.columns else None,
            marketplace=comment_scope,
            edited_global=edited["Global note"] if "Global note" in edited.columns else None,
        )

    # Status line + a Test button so the operator can poke Gist credentials
    # without having to edit a comment first.
    status = st.session_state.get("comments_status")
    gist_id, token = _gist_config()
    status_col, reload_col, test_col = st.columns([4, 1, 1])
    with status_col:
        if status:
            if "✓" in status:
                st.success(f"Comments {status}")
            elif "⚠" in status or "failed" in status:
                st.warning(f"Comments {status}")
            else:
                st.caption(f"Comments — {status}")
        elif not (gist_id and token):
            st.caption(
                "Comments are kept for this session only. To persist across "
                "container reboots, set `GITHUB_TOKEN` and `COMMENTS_GIST_ID` "
                "in the Streamlit Cloud secrets (see README)."
            )
    with reload_col:
        if gist_id and token and st.button(
            "↻ Reload",
            help=(
                "Re-fetch comments from the Gist now. Use this if you edited the "
                "Gist directly (e.g. pasted in a JSON dump) and want the dashboard "
                "to pick up the new content without rebooting."
            ),
            key="reload_comments_btn",
        ):
            st.session_state.pop("comments", None)
            st.session_state.pop("comments_status", None)
            st.rerun()
    with test_col:
        if gist_id and token and st.button(
            "Test Gist",
            help="Read + write round-trip against the configured Gist; reports the exact HTTP result.",
            key="test_gist_btn",
        ):
            data, read_detail = _load_comments_from_gist(gist_id, token)
            if data is None:
                st.session_state["comments_status"] = f"Gist read failed ({read_detail}) ⚠"
            else:
                # Round-trip the same payload so we exercise the write path too.
                ok, write_detail = _save_comments_to_gist(gist_id, token, data)
                if ok:
                    st.session_state["comments_status"] = "synced to Gist ✓  (test passed)"
                else:
                    st.session_state["comments_status"] = f"Gist write failed ({write_detail}) — session only ⚠"
            st.rerun()

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
        help="Snapshot of every comment. Save it as a backup or to seed another deploy.",
    )


# =========================================================================
# Margin Trend tab — CM3% trajectory bucketed by month / quarter / year
# =========================================================================
with tab_trend:
    # This tab spans the FULL available history (month/quarter/year trends
    # need a long horizon), honoring the marketplace + SKU + top-seller
    # filters but not the day/week period picker above.
    s_unit, s_thresh, s_info = st.columns([1.4, 1, 2.6])
    trend_unit = s_unit.radio(
        "Trend by", ["Month", "Quarter", "Year"],
        horizontal=True, index=0, key="trend_unit",
    )
    trend_freq = {"Month": "M", "Quarter": "Q", "Year": "Y"}[trend_unit]
    freq_label = trend_unit.lower()

    # Honor every top slicer: mp_slice is already marketplace- + revenue-gated,
    # and we now also restrict to the user's period selection. SKU search +
    # top-sellers re-applied here for symmetry with the Overview tab.
    trend_raw = mp_slice[mp_slice["Period"].isin(selected_periods)].copy()
    if sku_query.strip():
        _q = sku_query.strip().lower()
        trend_raw = trend_raw[
            trend_raw["SKU"].astype(str).str.lower().str.contains(_q, na=False)
            | trend_raw["Product"].astype(str).str.lower().str.contains(_q, na=False)
        ]
    if top_only and top_col and top_col in trend_raw.columns:
        trend_raw = trend_raw[trend_raw[top_col] == "Top Seller"]

    # Number of distinct buckets the chosen unit yields across the history.
    _n_buckets = (
        trend_raw["Period"].dropna().dt.to_period(trend_freq).nunique()
        if not trend_raw.empty else 0
    )

    if trend_raw.empty or _n_buckets < 2:
        st.info(
            f"Not enough data in the current selection to build a {freq_label} "
            f"trend (found {_n_buckets} {freq_label}(s) of data). Widen the "
            f"date selection in the top filter bar, or pick a finer unit "
            f"(e.g. Month)."
        )
    else:
        threshold_pp = s_thresh.number_input(
            "Trend threshold (± pp)", value=2.0, min_value=0.5, step=0.5,
            help="CM3% change from the first to the last bucket needed to count "
                 "as Rising or Declining. Smaller = more SKUs flagged.",
        )

        # ----- Portfolio CM3% trajectory -----
        port = _cm3_pct_by_bucket(trend_raw, trend_freq)
        # Aggregate across SKUs per bucket = Σ CM3 / Σ Sales, re-derived cleanly.
        r = trend_raw.dropna(subset=["Period"]).copy()
        r["bucket"] = r["Period"].dt.to_period(trend_freq).dt.start_time
        r["Product Sales"] = pd.to_numeric(r.get("Product Sales"), errors="coerce")
        if "CM3" in r.columns:
            r["CM3"] = pd.to_numeric(r["CM3"], errors="coerce")
            port_ts = r.groupby("bucket", as_index=False).agg(
                cm3=("CM3", "sum"), sales=("Product Sales", "sum")
            )
        else:
            r["_w"] = (pd.to_numeric(r["CM3%"], errors="coerce") * r["Product Sales"])
            port_ts = r.groupby("bucket", as_index=False).agg(
                cm3=("_w", "sum"), sales=("Product Sales", "sum")
            )
        port_ts["CM3%"] = port_ts["cm3"] / port_ts["sales"].where(port_ts["sales"] > 0) * 100
        port_ts = port_ts.sort_values("bucket")

        _span_start = pd.Timestamp(min(selected_periods)).strftime("%b %Y")
        _span_end = pd.Timestamp(max(selected_periods)).strftime("%b %Y")
        _span_label = _span_start if _span_start == _span_end else f"{_span_start} – {_span_end}"
        line = px.line(
            port_ts, x="bucket", y="CM3%", markers=True,
            title=f"Portfolio CM3% by {freq_label} — {marketplace} · {_span_label}",
        )
        line.update_yaxes(ticksuffix="%")
        line.update_xaxes(title=freq_label.capitalize())
        line.add_hline(
            y=target_cm3, line_dash="dot", line_color="#d32f2f",
            annotation_text=f"Target {target_cm3:.0f}%", annotation_position="top right",
        )
        st.plotly_chart(line, width="stretch")

        # ----- Per-SKU trend classification -----
        trends = margin_trend(trend_raw, trend_freq, threshold_pp=threshold_pp)
        if trends.empty:
            st.info(f"No SKU has at least two {freq_label}s of data in this selection.")
        else:
            counts = trends["Trend"].value_counts()
            n_rise = int(counts.get("Rising", 0))
            n_neutral = int(counts.get("Neutral", 0))
            n_decline = int(counts.get("Declining", 0))
            s_info.markdown(
                f"Classifying **{len(trends):,}** SKUs by CM3% change across "
                f"**{_n_buckets} {freq_label}s** (linear fit, first → last bucket)."
            )

            m1, m2, m3 = st.columns(3)
            m1.metric("📈 Rising margins", f"{n_rise:,}",
                      help=f"CM3% improved by ≥ {threshold_pp:.0f} pp over the window.")
            m2.metric("➡️ Neutral margins", f"{n_neutral:,}",
                      help=f"CM3% changed by less than ±{threshold_pp:.0f} pp.")
            m3.metric("📉 Declining margins", f"{n_decline:,}",
                      help=f"CM3% dropped by ≥ {threshold_pp:.0f} pp over the window.")

            # Distribution bar
            dist = pd.DataFrame({
                "Trend": ["Rising", "Neutral", "Declining"],
                "SKUs": [n_rise, n_neutral, n_decline],
            })
            bar = px.bar(
                dist, x="Trend", y="SKUs", color="Trend",
                color_discrete_map={
                    "Rising": "#1B7A3D", "Neutral": "#9E9E9E", "Declining": "#C62828",
                },
                text="SKUs",
            )
            bar.update_layout(showlegend=False, height=260, margin=dict(t=10, b=10))
            st.plotly_chart(bar, width="stretch")

            # ----- Per-category SKU tables -----
            # Enrich with Product + latest sales/CM3 from the aggregated view.
            enrich = filtered.set_index("SKU")
            for col in ["Product", "Product Sales", "CM3%", "Cluster"]:
                if col in enrich.columns:
                    trends[col] = trends["SKU"].map(enrich[col])

            # Label the start/end bucket per SKU according to the chosen unit.
            def _fmt_bucket(ts):
                if pd.isna(ts):
                    return "—"
                ts = pd.Timestamp(ts)
                if trend_freq == "M":
                    return ts.strftime("%b %Y")
                if trend_freq == "Q":
                    return f"Q{ts.quarter} {ts.year}"
                return str(ts.year)
            trends["From"] = trends["Start period"].map(_fmt_bucket)
            trends["To"] = trends["End period"].map(_fmt_bucket)

            trend_order = {
                "📈 Rising margins": ("Rising", False),
                "📉 Declining margins": ("Declining", True),
                "➡️ Neutral margins": ("Neutral", None),
            }
            for label, (key, asc) in trend_order.items():
                sub = trends[trends["Trend"] == key].copy()
                if sub.empty:
                    continue
                if asc is None:
                    sub = sub.sort_values("Change", key=lambda s: s.abs())
                else:
                    sub = sub.sort_values("Change", ascending=asc)
                show_cols = [c for c in [
                    "SKU", "Product", "Cluster", "Product Sales",
                    "From", "Start CM3%", "To", "End CM3%", "Change", "Points",
                ] if c in sub.columns]
                with st.expander(f"{label} ({len(sub):,})", expanded=(key != "Neutral")):
                    st.dataframe(
                        sub[show_cols].style.format({
                            "Product Sales": "€{:,.0f}",
                            "Start CM3%": "{:.1f}%",
                            "End CM3%": "{:.1f}%",
                            "Change": "{:+.1f} pp",
                            "Points": "{:.0f}",
                        }, na_rep="—"),
                        width="stretch", hide_index=True,
                        column_config={
                            "From": st.column_config.TextColumn(
                                "From", help="First " + freq_label + " with data for this SKU"),
                            "To": st.column_config.TextColumn(
                                "To", help="Last " + freq_label + " with data for this SKU"),
                        },
                    )
                    st.download_button(
                        f"Download {key} SKUs (CSV)",
                        data=sub[show_cols].to_csv(index=False).encode("utf-8"),
                        file_name=f"margin_{key.lower()}_{marketplace}_{pd.Timestamp(period):%Y%m%d}.csv",
                        mime="text/csv",
                        key=f"dl_trend_{key}",
                    )

            st.caption(
                f"Trend = linear fit of CM3% across the {freq_label}s; "
                f"'Change' is the predicted first→last difference in percentage "
                f"points. **Start CM3% is measured in the 'From' {freq_label} and "
                f"End CM3% in the 'To' {freq_label}** (each SKU's first/last "
                f"{freq_label} with data — these can differ between SKUs if a "
                f"product only sold for part of the history). SKUs with fewer "
                f"than two {freq_label}s of data are omitted."
            )

# =========================================================================
# Slow movers tab — SKUs with Days of Supply above threshold (default 180)
# =========================================================================
with tab_slow:
    if "Days of Supply" not in filtered.columns:
        st.info("No inventory data available — Days of Supply column missing.")
    else:
        s1, s2 = st.columns([1, 3])
        dos_threshold = s1.number_input(
            "Days of Supply >",
            min_value=30, value=180, step=30,
            help="Tage Reach — wie viele Tage Bestand bei aktueller Velocity. "
                 "Standard 180 = mehr als 6 Monate Lager.",
        )

        slow = filtered.copy()
        slow["Days of Supply"] = pd.to_numeric(slow["Days of Supply"], errors="coerce")
        slow = slow[slow["Days of Supply"] > dos_threshold].copy()

        if slow.empty:
            s2.markdown(
                f"&nbsp; No SKUs with Days of Supply > {dos_threshold} in the current view. "
                f"Either inventory is healthy, the filters are too narrow, or the Days of "
                f"Supply column isn't populated by Novadata yet (known gap)."
            )
        else:
            # Tied-up stock value: FBA Available × avg unit price (sales / units over period)
            units = pd.to_numeric(slow.get("Units"), errors="coerce")
            sales = pd.to_numeric(slow.get("Product Sales"), errors="coerce")
            fba = pd.to_numeric(slow.get("FBA Available"), errors="coerce")
            unit_price = sales / units.where(units > 0)
            slow["Avg unit price"] = unit_price
            slow["Tied-up value"] = fba * unit_price
            slow = slow.sort_values("Days of Supply", ascending=False)

            s2.markdown(
                f"**{len(slow):,} SKUs** mit > {dos_threshold} Tagen Reach. "
                f"Median {slow['Days of Supply'].median():,.0f} d, "
                f"Max {slow['Days of Supply'].max():,.0f} d."
            )

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Slow-mover SKUs", f"{len(slow):,}")
            k2.metric("FBA units locked", f"{int(fba.sum()):,}" if fba.notna().any() else "—")
            tied = slow["Tied-up value"].sum()
            k3.metric("Tied-up stock value (≈€)",
                      f"€{tied:,.0f}" if pd.notna(tied) and tied > 0 else "—",
                      help="FBA Available × avg unit price (sales / units over the selected period).")
            avg_cm3_slow = pd.to_numeric(slow.get("CM3%"), errors="coerce").mean()
            k4.metric("Avg CM3 %", f"{avg_cm3_slow:.1f}%" if pd.notna(avg_cm3_slow) else "—")

            # Pull current comments (same store the Overview tab edits) so any
            # note added here ↔ shows up on the other tab automatically.
            if "comments" not in st.session_state:
                st.session_state["comments"] = load_comments()
            slow_scope = "__all__" if is_all_countries else marketplace
            slow["Comments"] = [
                comment_for_view(sku, slow_scope, st.session_state["comments"])
                for sku in slow["SKU"]
            ]
            slow["Global note"] = [
                global_note_for(sku, st.session_state["comments"])
                for sku in slow["SKU"]
            ]

            show_cols = [c for c in [
                "SKU", "Product", "Cluster",
                "FBA Available", "Sales Velocity", "Days of Supply",
                "Avg unit price", "Tied-up value",
                "Product Sales", "Units", "CM3%",
                "Comments", "Global note",
            ] if c in slow.columns]

            slow_view = slow[show_cols].reset_index(drop=True)
            slow_edited = st.data_editor(
                slow_view,
                width="stretch", hide_index=True, height=520,
                key="slow_movers_editor",
                column_config={
                    "SKU": st.column_config.TextColumn("SKU", disabled=True),
                    "Product": st.column_config.TextColumn("Product", disabled=True, width="large"),
                    "Cluster": st.column_config.TextColumn("Cluster", disabled=True),
                    "FBA Available": st.column_config.NumberColumn("FBA Avail.", format="%d", disabled=True),
                    "Sales Velocity": st.column_config.NumberColumn("Velocity / d", format="%.1f", disabled=True),
                    "Days of Supply": st.column_config.NumberColumn(
                        "Days of Supply", format="%d", disabled=True,
                        help=f"Orange ≥ {dos_threshold} d (your threshold); red ≥ {dos_threshold*2} d (critical).",
                    ),
                    "Avg unit price": st.column_config.NumberColumn("Avg unit price", format="€%.1f", disabled=True),
                    "Tied-up value": st.column_config.NumberColumn("Tied-up value", format="€%d", disabled=True),
                    "Product Sales": st.column_config.NumberColumn("Sales (€)", format="€%d", disabled=True),
                    "Units": st.column_config.NumberColumn("Units", format="%d", disabled=True),
                    "CM3%": st.column_config.NumberColumn("CM3 %", format="%.1f%%", disabled=True),
                    "Comments": st.column_config.TextColumn(
                        (f"Comments — all countries (read-only)" if is_all_countries
                         else f"Comments — {_country_tag(marketplace)} ✏"),
                        help=(
                            "Read-only in All countries view (concatenated note from every "
                            "marketplace). Pick a single country to edit." if is_all_countries
                            else "Free-text note per SKU + country. Synced with the Overview tab."),
                        disabled=is_all_countries,
                        width="medium",
                    ),
                    "Global note": st.column_config.TextColumn(
                        "🌍 Global note ✏",
                        help="Cross-country note for this SKU (visible in every view, edit anywhere).",
                        disabled=False,
                        width="medium",
                    ),
                },
            )

            # Persist any comment edits (per-country AND global) through the
            # shared helper so the Overview tab sees the same store.
            if "SKU" in slow_edited.columns:
                persist_comment_edits(
                    slow_edited["SKU"],
                    edited_comments=slow_edited["Comments"] if "Comments" in slow_edited.columns else None,
                    marketplace=slow_scope,
                    edited_global=slow_edited["Global note"] if "Global note" in slow_edited.columns else None,
                )

            st.download_button(
                "Download slow movers (CSV)",
                data=slow[show_cols].to_csv(index=False).encode("utf-8"),
                file_name=f"slow_movers_{marketplace}_{pd.Timestamp(period):%Y%m%d}.csv",
                mime="text/csv",
            )

            st.caption(
                "Days of Supply = FBA Available ÷ Sales Velocity (units/day). The orange shade "
                f"marks SKUs above {dos_threshold} d; the deeper red above {dos_threshold*2} d "
                "(critical overstock). 'Tied-up value' is FBA units × the SKU's average unit "
                "price over the selected period — a rough estimate of cash sitting in stock."
            )
