"""Vanatari corporate design system for the Margin Analytics dashboard.

Single source of truth for brand colour + typography, applied to Streamlit
chrome (CSS) and to Plotly charts (a registered template). Values come from the
Vanatari Corporate Design Booklet (2026).

Usage (once, right after ``st.set_page_config``)::

    import brand
    brand.apply()

Then build Plotly figures as usual — the default template + colourway are set.
For explicit series colours use ``brand.CHART[...]`` / ``brand.CATEGORICAL``.
"""
from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

# ---------- Brand palette (booklet values) ----------
PLUM = "#3c1826"        # Primary — text / ink
ORANGE = "#ff5c3e"      # Primary — highlight / accent
LIGHT_BLUE = "#e3eef6"  # Secondary — background / elements
BEIGE = "#fbf7f2"       # Secondary — background / elements
PINK = "#f5c0fb"        # Extended accent
BLUE = "#6c91cc"        # Extended accent

# Neutral ink tints derived from Dark Plum (for muted text / gridlines).
PLUM_60 = "#8a7580"     # ~60% plum on white — muted text
PLUM_GRID = "#ece3e7"   # very light plum tint — recessive gridlines

# ---------- Data-safe chart hues ----------
# The booklet hues are tuned for decoration; at mark size they fail the
# data-viz checks (lightness band, chroma floor, contrast). These variants keep
# each brand hue but pass colour-blind separation + >=3:1 contrast on the beige
# surface (validated). Order = fixed categorical assignment, never cycled.
CHART_ORANGE = "#ec4a26"   # Warm Orange, data-safe (primary series)
CHART_BLUE = "#4569ad"     # Secondary Blue, data-safe
CHART_BERRY = "#a83f63"    # Dark Plum -> berry, data-safe
CHART_VIOLET = "#b552bf"   # Pink -> violet, data-safe
CATEGORICAL = [CHART_ORANGE, CHART_BLUE, CHART_BERRY, CHART_VIOLET]

# Convenience accessor mirroring the categorical order.
CHART = {
    "orange": CHART_ORANGE,
    "blue": CHART_BLUE,
    "berry": CHART_BERRY,
    "violet": CHART_VIOLET,
}

# ---------- Semantic status colours (reserved — not brand series) ----------
GOOD = "#2e7d4f"
WARN = "#e88a3c"
BAD = "#c62828"
NEUTRAL = "#9a8f94"

SANS = "Satoshi, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
SERIF = "Literata, Georgia, 'Times New Roman', serif"

_TEMPLATE_NAME = "vanatari"


def _register_template() -> None:
    tpl = go.layout.Template()
    tpl.layout.colorway = CATEGORICAL
    tpl.layout.font = dict(family=SANS, color=PLUM, size=13)
    tpl.layout.title = dict(font=dict(family=SERIF, color=PLUM, size=17))
    tpl.layout.paper_bgcolor = "rgba(0,0,0,0)"
    tpl.layout.plot_bgcolor = "rgba(0,0,0,0)"
    tpl.layout.xaxis = dict(
        gridcolor=PLUM_GRID, linecolor=PLUM_GRID, zerolinecolor=PLUM_GRID,
        tickfont=dict(color=PLUM_60), title=dict(font=dict(color=PLUM_60)),
    )
    tpl.layout.yaxis = dict(
        gridcolor=PLUM_GRID, linecolor=PLUM_GRID, zerolinecolor=PLUM_GRID,
        tickfont=dict(color=PLUM_60), title=dict(font=dict(color=PLUM_60)),
    )
    tpl.layout.legend = dict(font=dict(color=PLUM))
    tpl.layout.colorscale = dict(sequential=[[0.0, BEIGE], [1.0, CHART_ORANGE]])
    pio.templates[_TEMPLATE_NAME] = tpl
    pio.templates.default = _TEMPLATE_NAME
    px.defaults.template = _TEMPLATE_NAME
    px.defaults.color_discrete_sequence = CATEGORICAL


def _css() -> str:
    return f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Literata:opsz,wght@7..72,400;7..72,500;7..72,600&display=swap');
    @import url('https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700&display=swap');

    :root {{
      --van-plum: {PLUM}; --van-orange: {ORANGE};
      --van-light-blue: {LIGHT_BLUE}; --van-beige: {BEIGE};
    }}

    html, body, [class*="css"], .stApp, .stMarkdown, p, label, span, div {{
      font-family: {SANS};
    }}
    .stApp {{ background-color: {BEIGE}; color: {PLUM}; }}

    /* Headlines in the serif; app title gets brand accent */
    h1, h2, h3, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {{
      font-family: {SERIF} !important;
      color: {PLUM} !important;
      letter-spacing: -0.01em;
    }}
    h1 {{ font-weight: 600 !important; }}

    /* Sidebar on the light-blue surface */
    section[data-testid="stSidebar"] {{ background-color: {LIGHT_BLUE}; }}

    /* Tabs — active tab carries the warm-orange accent */
    .stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {PLUM_GRID}; }}
    .stTabs [data-baseweb="tab"] {{ color: {PLUM_60}; font-weight: 500; }}
    .stTabs [aria-selected="true"] {{ color: {PLUM} !important; }}
    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] > div {{ background-color: {ORANGE} !important; }}

    /* Metric cards */
    [data-testid="stMetric"] {{
      background: #ffffff; border: 1px solid {PLUM_GRID};
      border-radius: 12px; padding: 14px 16px;
    }}
    [data-testid="stMetricValue"] {{ font-family: {SERIF}; color: {PLUM}; }}
    [data-testid="stMetricLabel"] {{ color: {PLUM_60}; }}

    /* Primary buttons in warm orange */
    .stButton > button[kind="primary"],
    .stFormSubmitButton > button {{
      background-color: {ORANGE}; border-color: {ORANGE}; color: #ffffff;
      border-radius: 10px; font-weight: 600;
    }}
    .stButton > button[kind="primary"]:hover,
    .stFormSubmitButton > button:hover {{
      background-color: {PLUM}; border-color: {PLUM}; color: #ffffff;
    }}

    /* Links + captions */
    a, a:visited {{ color: {CHART_ORANGE}; }}
    .stCaption, [data-testid="stCaptionContainer"] {{ color: {PLUM_60}; }}
    </style>
    """


def apply() -> None:
    """Register the Plotly template and inject the brand CSS. Idempotent."""
    _register_template()
    st.markdown(_css(), unsafe_allow_html=True)
