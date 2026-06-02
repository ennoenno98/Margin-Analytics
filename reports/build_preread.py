"""Build the March–May 2026 margin-review preread PDF (all-country level).

Reusable: pulls the latest margin export, recomputes the four focus segments
and the margin-trend leaderboards, renders charts, and writes a multi-page PDF.

    python reports/build_preread.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from streamlit_app import (  # noqa: E402
    load_data, latest_export, aggregate_periods, tier_1_to_3, margin_trend,
    latest_products_export, load_products,
)

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak,
    KeepTogether,
)

# ─── Vegavero corporate palette ───────────────────────────────────────
NAVY = colors.HexColor("#205F2A")   # brand green (primary chrome / headers)
GREEN = colors.HexColor("#2E7D32")  # semantic "improving"
RED = colors.HexColor("#C62828")    # semantic "declining"
CORAL = colors.HexColor("#F5758D")  # brand coral accent
AMBER = colors.HexColor("#B26A00")
LIGHT = colors.HexColor("#EBF3D1")  # brand light-green (tiles / alt rows)
CREAM = colors.HexColor("#FFFBF2")  # brand background
INK = colors.HexColor("#2A2A2A")    # body text on cream
GREY = colors.HexColor("#6B6B6B")

ASSETS = REPO / "reports" / "assets"
LOGO = ASSETS / "vegavero_logo.png"
BLOB = ASSETS / "blob_light.png"
OUT = REPO / "reports" / "Margin_Review_Mar-May_2026.pdf"
CHART_DIR = REPO / "reports" / "_charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)

# Chart styling to match the brand
plt.rcParams.update({
    "font.family": "DejaVu Sans",  # Calibri not available on the build box
    "axes.edgecolor": "#205F2A",
    "axes.labelcolor": "#2A2A2A",
    "text.color": "#2A2A2A",
    "xtick.color": "#2A2A2A",
    "ytick.color": "#2A2A2A",
    "figure.facecolor": "#FFFBF2",
    "axes.facecolor": "#FFFBF2",
    "savefig.facecolor": "#FFFBF2",
})

WINDOW_LABEL = "March – May 2026"


def _page_bg(canvas, doc, title=False):
    """Cream background + Vegavero logo on every page; blob accent on title."""
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(CREAM)
    canvas.rect(0, 0, w, h, stroke=0, fill=1)
    if title:
        # Light-green organic blob, top-right, behind the title block.
        try:
            canvas.drawImage(str(BLOB), w - 9.5 * cm, h - 7.5 * cm,
                             width=10.5 * cm, height=6 * cm, mask="auto",
                             preserveAspectRatio=True, anchor="ne")
        except Exception:
            pass
    # Logo top-left
    try:
        canvas.drawImage(str(LOGO), 1.6 * cm, h - 1.7 * cm,
                         width=3.2 * cm, height=1.18 * cm, mask="auto",
                         preserveAspectRatio=True, anchor="nw")
    except Exception:
        pass
    # Footer
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(GREY)
    canvas.drawString(1.6 * cm, 0.9 * cm, "Vegavero · Margin Review")
    canvas.drawRightString(w - 1.6 * cm, 0.9 * cm, f"{WINDOW_LABEL}  ·  p.{doc.page}")
    canvas.setStrokeColor(LIGHT)
    canvas.setLineWidth(1)
    canvas.line(1.6 * cm, 1.2 * cm, w - 1.6 * cm, 1.2 * cm)
    canvas.restoreState()


def _on_first(canvas, doc):
    _page_bg(canvas, doc, title=True)


def _on_later(canvas, doc):
    _page_bg(canvas, doc, title=False)


def eur(x):
    return f"€{x:,.0f}" if pd.notna(x) else "—"


def pct(x):
    return f"{x:.1f}%" if pd.notna(x) else "—"


# ─── Compute ──────────────────────────────────────────────────────────
def compute():
    df = load_data(latest_export(REPO / "novadata_exports"))
    start = pd.Timestamp("2026-03-01")
    end = pd.Timestamp("2026-05-31")
    win = df[(df["Period"] >= start) & (df["Period"] <= end)].copy()

    agg = aggregate_periods(win)
    agg = agg[agg["Product Sales"] > 0].copy()

    tb = agg.dropna(subset=["CM3%", "Product Sales"]).copy()
    tb["MT"] = tier_1_to_3(tb["CM3%"])
    tb["VT"] = tier_1_to_3(tb["Product Sales"])
    tb["C"] = tb["MT"].astype("string") + "-" + tb["VT"].astype("string")

    # Monthly portfolio CM3% (all-country)
    w = win.dropna(subset=["Period"]).copy()
    w["month"] = w["Period"].dt.to_period("M").dt.to_timestamp()
    w["CM3"] = pd.to_numeric(w["CM3"], errors="coerce")
    w["Product Sales"] = pd.to_numeric(w["Product Sales"], errors="coerce")
    port = w.groupby("month", as_index=False).agg(cm3=("CM3", "sum"), sales=("Product Sales", "sum"))
    port["CM3%"] = port["cm3"] / port["sales"] * 100

    # Margin trend, monthly, all-country, materiality floor €10k over window
    trends = margin_trend(win, "M", threshold_pp=2.0)
    enr = agg.set_index("SKU")
    for c in ["Product", "Product Sales", "CM1%", "CM2%", "CM3", "CM3%"]:
        if c in enr.columns:
            trends[c] = trends["SKU"].map(enr[c])
    trends = trends[(trends["Points"] >= 2) & (trends["Product Sales"] >= 10000)].copy()

    cm3_cuts = np.round(np.nanpercentile(tb["CM3%"].astype(float), [33.33, 66.67]), 1)
    sales_cuts = np.round(np.nanpercentile(tb["Product Sales"].astype(float), [33.33, 66.67]), 0)

    # Slow movers: latest inventory snapshot from the products feed, all-country
    # sum, Days of Supply = FBA Available / Sales Velocity. Threshold 180 days.
    slow = pd.DataFrame()
    pp_path = latest_products_export(REPO / "novadata_exports")
    if pp_path is not None:
        pp = load_products(pp_path)
        for col in ("FBA Available", "Sales Velocity"):
            if col in pp.columns:
                pp[col] = pd.to_numeric(pp[col], errors="coerce")
        # Sum FBA + Velocity across marketplaces, then derive DoS from the totals.
        sku_inv = pp.groupby("SKU", as_index=False).agg(
            fba=("FBA Available", "sum"),
            vel=("Sales Velocity", "sum"),
        )
        sku_inv["dos"] = sku_inv["fba"] / sku_inv["vel"].where(sku_inv["vel"] > 0)
        # Join with Mar-May economics (sales, units, CM3%) for ranking + value.
        slow = sku_inv.merge(
            agg[["SKU", "Product", "Product Sales", "Units", "CM3%"]],
            on="SKU", how="left",
        )
        slow = slow[slow["dos"] > 180].copy()
        # Tied-up value ≈ FBA × avg unit price over the period.
        units = pd.to_numeric(slow["Units"], errors="coerce")
        sales = pd.to_numeric(slow["Product Sales"], errors="coerce")
        slow["unit_price"] = sales / units.where(units > 0)
        slow["tied_up"] = slow["fba"] * slow["unit_price"]
        slow = slow.sort_values("dos", ascending=False).reset_index(drop=True)

    return dict(agg=agg, tb=tb, port=port, trends=trends, slow=slow,
                cm3_cuts=cm3_cuts, sales_cuts=sales_cuts)


def cluster_stats(tb, code):
    s = tb[tb["C"] == code]
    sales = s["Product Sales"].sum()
    cm1 = s["CM1"].sum() if "CM1" in s.columns else np.nan
    cm2 = s["CM2"].sum() if "CM2" in s.columns else np.nan
    cm3 = s["CM3"].sum()
    pc = (lambda v: v / sales * 100 if sales else np.nan)
    return dict(n=len(s), sales=sales, cm1=cm1, cm2=cm2, cm3=cm3,
                cm1pct=pc(cm1), cm2pct=pc(cm2), cm3pct=pc(cm3), rows=s)


# ─── Charts ───────────────────────────────────────────────────────────
def chart_matrix(tb):
    margins = {1: "High margin", 2: "Mid margin", 3: "Low margin"}
    vols = {1: "High sales", 2: "Mid sales", 3: "Low sales"}
    sales_grid = np.zeros((3, 3)); cm3_grid = np.full((3, 3), np.nan); n_grid = np.zeros((3, 3))
    for mi, m in enumerate([1, 2, 3]):
        for vi, v in enumerate([1, 2, 3]):
            st = cluster_stats(tb, f"{m}-{v}")
            sales_grid[mi, vi] = st["sales"]; cm3_grid[mi, vi] = st["cm3pct"]; n_grid[mi, vi] = st["n"]
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    im = ax.imshow(cm3_grid, cmap="RdYlGn", vmin=-15, vmax=30, aspect="auto")
    ax.set_xticks(range(3)); ax.set_xticklabels([vols[v] for v in [1, 2, 3]])
    ax.set_yticks(range(3)); ax.set_yticklabels([margins[m] for m in [1, 2, 3]])
    for mi in range(3):
        for vi in range(3):
            ax.text(vi, mi - 0.16, f"{int(n_grid[mi,vi])} SKUs", ha="center", va="center", fontsize=9, color="#111")
            ax.text(vi, mi + 0.06, f"€{sales_grid[mi,vi]/1000:,.0f}k", ha="center", va="center", fontsize=11, fontweight="bold", color="#111")
            ax.text(vi, mi + 0.28, f"{cm3_grid[mi,vi]:.1f}% CM3", ha="center", va="center", fontsize=9, color="#222")
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04); cb.set_label("Blended CM3%")
    ax.set_title("Margin × Sales matrix — sales and blended CM3% per segment", fontsize=11, fontweight="bold", pad=10)
    fig.tight_layout()
    p = CHART_DIR / "matrix.png"; fig.savefig(p, dpi=150); plt.close(fig)
    return p


def chart_portfolio(port):
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    x = [d.strftime("%b %Y") for d in port["month"]]
    ax.plot(x, port["CM3%"], marker="o", color="#205F2A", linewidth=2.6, markersize=9,
            markerfacecolor="#205F2A", markeredgecolor="#FFFBF2")
    for xi, yi in zip(x, port["CM3%"]):
        ax.annotate(f"{yi:.1f}%", (xi, yi), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=10, fontweight="bold")
    ax.axhline(19.7, ls=":", color="#C62828", lw=1.4)
    ax.text(len(x) - 1, 19.7, " Target 19.7%", color="#C62828", va="bottom", ha="right", fontsize=8)
    ax.set_ylabel("Portfolio CM3%"); ax.grid(axis="y", alpha=0.3)
    ax.set_title("Blended portfolio CM3% by month (all countries)", fontsize=11, fontweight="bold")
    fig.tight_layout()
    p = CHART_DIR / "portfolio.png"; fig.savefig(p, dpi=150); plt.close(fig)
    return p


# ─── PDF ──────────────────────────────────────────────────────────────
def build():
    d = compute()
    tb, agg, port, trends = d["tb"], d["agg"], d["port"], d["trends"]

    total_sales = agg["Product Sales"].sum()
    total_cm3 = agg["CM3"].sum()
    blended = total_cm3 / total_sales * 100

    focus = {
        "Low margin · High sales": cluster_stats(tb, "3-1"),
        "Low margin · Medium sales": cluster_stats(tb, "3-2"),
        "High margin · Low sales": cluster_stats(tb, "1-3"),
        "High margin · High sales": cluster_stats(tb, "1-1"),
    }

    m_png = chart_matrix(tb)
    p_png = chart_portfolio(port)

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=NAVY, fontSize=26,
                        spaceAfter=4, alignment=0, spaceBefore=18)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=GREY, fontSize=10.5,
                         spaceAfter=2, alignment=0)
    tag = ParagraphStyle("tag", parent=styles["Normal"], textColor=colors.white,
                         fontSize=8.5, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=NAVY, fontSize=14, spaceBefore=12, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], textColor=NAVY, fontSize=11.5, spaceBefore=8, spaceAfter=3)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14,
                          spaceAfter=4, textColor=INK)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=GREY)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=12, bulletIndent=2)

    # Extra top margin on every page so content clears the logo band.
    doc = SimpleDocTemplate(str(OUT), pagesize=A4,
                            leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=2.3 * cm, bottomMargin=1.6 * cm,
                            title="Margin Review — March to May 2026")
    E = []

    # ----- Title + exec summary (headline left, brand blob right) -----
    tagtab = Table([[Paragraph("MARGIN REVIEW", tag)]], colWidths=[3.6 * cm])
    tagtab.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CORAL),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    E.append(tagtab)
    E.append(Spacer(1, 6))
    E.append(Paragraph("Margin Review", h1))
    E.append(Paragraph(f"{WINDOW_LABEL} &nbsp;·&nbsp; all marketplaces &nbsp;·&nbsp; "
                       f"prepared for Product, Procurement &amp; Marketing", sub))
    E.append(Spacer(1, 12))

    kpi = [["Net sales", "Contribution Margin 3", "Blended CM3%", "Active SKUs"],
           [eur(total_sales), eur(total_cm3), pct(blended), f"{len(agg):,}"]]
    t = Table(kpi, colWidths=[4.3 * cm] * 4)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("FONTSIZE", (0, 1), (-1, 1), 15),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 1), (-1, 1), NAVY),
        ("BACKGROUND", (0, 1), (-1, 1), LIGHT),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 2, colors.white),
    ]))
    E.append(t)
    E.append(Spacer(1, 12))

    E.append(Paragraph("Why we're meeting", h2))
    E.append(Paragraph(
        f"Over {WINDOW_LABEL} the catalogue generated <b>{eur(total_sales)}</b> in net sales at a "
        f"<b>blended CM3 of {pct(blended)}</b> — well below the {pct(19.7)} target. The headline gap "
        f"is concentrated, not spread evenly: a small group of high-revenue, low-margin SKUs is "
        f"diluting the whole portfolio, while a healthy high-margin core is being under-scaled. "
        f"This preread frames four product segments and the biggest month-over-month margin movers "
        f"so Product, Procurement and Marketing can align on where to act first.", body))

    low_high = focus["Low margin · High sales"]
    high_high = focus["High margin · High sales"]
    high_low = focus["High margin · Low sales"]
    E.append(Paragraph("Headline takeaways", h3))
    for b in [
        f"<b>The margin drag is concentrated.</b> {low_high['n']} 'low-margin / high-sales' SKUs "
        f"turn over {eur(low_high['sales'])} ({low_high['sales']/total_sales*100:.0f}% of sales) at "
        f"just {pct(low_high['cm3pct'])} CM3 — they earn {eur(low_high['cm3'])} of contribution. "
        f"Fixing these is the single largest lever.",
        f"<b>The profitable core is solid but small.</b> {high_high['n']} 'high-margin / high-sales' "
        f"SKUs deliver {eur(high_high['cm3'])} of CM3 at {pct(high_high['cm3pct'])} — protect price and "
        f"availability here at all costs.",
        f"<b>There is untapped upside.</b> {high_low['n']} 'high-margin / low-sales' SKUs already run "
        f"at {pct(high_low['cm3pct'])} CM3 but only {eur(high_low['sales'])} of sales — a marketing / "
        f"merchandising scale opportunity with no margin risk.",
        f"<b>Margins are trending down portfolio-wide.</b> Blended CM3 fell from "
        f"{pct(port['CM3%'].iloc[0])} in {port['month'].iloc[0].strftime('%b')} to "
        f"{pct(port['CM3%'].iloc[-1])} in {port['month'].iloc[-1].strftime('%b')}; the bottom-10 "
        f"decliners (p.4) explain most of the slide.",
    ]:
        E.append(Paragraph(b, bullet, bulletText="•"))

    E.append(PageBreak())

    # ----- Matrix page -----
    E.append(Paragraph("Margin × Sales matrix", h2))
    E.append(Paragraph(
        "Every SKU is placed in a 3×3 grid: thirds by all-country CM3% (margin) and thirds by "
        "all-country net sales (volume). Cells show SKU count, total sales and the segment's "
        "blended CM3%.", body))
    E.append(Image(str(m_png), width=16.5 * cm, height=10.5 * cm))
    E.append(Spacer(1, 6))
    E.append(Paragraph(
        f"Tier cut-offs (terciles): margin at {d['cm3_cuts'][0]:.1f}% and {d['cm3_cuts'][1]:.1f}% CM3; "
        f"sales at {eur(d['sales_cuts'][0])} and {eur(d['sales_cuts'][1])} over the three months.", small))
    E.append(PageBreak())

    # ----- Four focus segments -----
    E.append(Paragraph("The four segments to discuss", h2))
    E.append(Paragraph(
        "Each segment lists its five most P&amp;L-relevant SKUs — for the high-margin segments "
        "these are the biggest CM3 contributors; for the low-margin segments, the biggest CM3 "
        "drains. The prompts are starting points for the room, not fixed actions.", body))
    E.append(Spacer(1, 4))

    # (colour, verdict, open prompts, sort-ascending-by-CM3?)
    # Low-margin segments → ascending (most-negative P&L first = biggest drains).
    # High-margin segments → descending (biggest P&L contributors first).
    seg_meta = {
        "Low margin · High sales": (RED,
            "Biggest lever — high volume at thin or negative margin.",
            "Where is the margin going: landed cost, fulfilment, or discount/ad spend? "
            "Which of these are structural vs. fixable, and which is volume worth keeping at all?",
            True),
        "Low margin · Medium sales": (AMBER,
            "Watch list — weak margin before they scale further.",
            "Are these on a path to the high-sales / low-margin trap, and what would it take to "
            "turn the margin before they grow?",
            True),
        "High margin · Low sales": (GREEN,
            "Scale opportunity — strong margin, under-exposed.",
            "What is capping demand — visibility, content, availability, price? Where could extra "
            "investment pay back given the healthy margin?",
            False),
        "High margin · High sales": (NAVY,
            "Protect — the profit engine of the catalogue.",
            "What are the risks to these (price pressure, stock-outs, competitor entry), and how "
            "much headroom is there before margin erodes?",
            False),
    }
    for name, st in focus.items():
        col, verdict, prompt, asc = seg_meta[name]
        block = [Paragraph(name, ParagraphStyle("seg", parent=h3, textColor=col))]
        line = (f"<b>{st['n']} SKUs</b> &nbsp;|&nbsp; {eur(st['sales'])} sales "
                f"({st['sales']/total_sales*100:.0f}% of total) &nbsp;|&nbsp; "
                f"blended margin <b>CM1 {pct(st['cm1pct'])} &nbsp;›&nbsp; "
                f"CM2 {pct(st['cm2pct'])} &nbsp;›&nbsp; CM3 {pct(st['cm3pct'])}</b>")
        block.append(Paragraph(line, body))
        block.append(Paragraph(f"<i>{verdict}</i> &nbsp; {prompt}", body))

        # Five most P&L-relevant SKUs (CM3 €), direction per segment.
        ex = st["rows"].sort_values("CM3", ascending=asc).head(5)
        if len(ex):
            data = [["Product", "P&L (CM3 €)", "Sales", "CM1%", "CM2%", "CM3%"]]
            for _, r in ex.iterrows():
                data.append([
                    str(r["Product"])[:42],
                    eur(r["CM3"]), eur(r["Product Sales"]),
                    pct(r.get("CM1%")), pct(r.get("CM2%")), pct(r["CM3%"]),
                ])
            t = Table(data, colWidths=[6.6 * cm, 2.4 * cm, 2.2 * cm,
                                       1.5 * cm, 1.5 * cm, 1.5 * cm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), col),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]))
            block.append(Spacer(1, 3))
            block.append(t)
        block.append(Spacer(1, 9))
        E.append(KeepTogether(block))

    # ----- Trend leaderboards -----
    E.append(Spacer(1, 4))
    E.append(KeepTogether([
        Paragraph("Margin trend — biggest movers", h2),
        Paragraph(
            "Ranked by CM3% change from March to May per SKU (all-country, sales-weighted linear "
            "fit), limited to SKUs with at least €10,000 of net sales over the window. CM1% and "
            "CM2% are the period-blended levels for context — comparing them with the CM3 "
            "trajectory shows whether a move came from COGS (CM1) or from fulfilment / ad cost "
            "downstream.", body),
        Image(str(p_png), width=15.5 * cm, height=6.9 * cm),
    ]))
    E.append(Spacer(1, 6))

    def trend_table(rows, ascending, color):
        rows = rows.sort_values("Change", ascending=ascending).head(10)
        data = [["Product", "Sales", "CM1%", "CM2%", "Mar CM3", "May CM3", "Δ CM3"]]
        for _, r in rows.iterrows():
            data.append([
                str(r["Product"])[:38],
                eur(r["Product Sales"]),
                pct(r.get("CM1%")), pct(r.get("CM2%")),
                pct(r["Start CM3%"]),
                pct(r["End CM3%"]),
                f"{r['Change']:+.1f}",
            ])
        t = Table(data, colWidths=[6.0 * cm, 2.1 * cm, 1.4 * cm, 1.4 * cm,
                                   1.6 * cm, 1.6 * cm, 1.4 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), color),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.white),
        ]))
        return t

    E.append(KeepTogether([
        Paragraph("Top 10 improving", ParagraphStyle("g", parent=h3, textColor=GREEN)),
        trend_table(trends, ascending=False, color=GREEN),
    ]))
    E.append(Spacer(1, 10))
    E.append(KeepTogether([
        Paragraph("Bottom 10 declining", ParagraphStyle("r", parent=h3, textColor=RED)),
        trend_table(trends, ascending=True, color=RED),
    ]))

    # ----- Slow movers ------------------------------------------------------
    slow = d["slow"]
    if not slow.empty:
        E.append(PageBreak())
        E.append(Paragraph("Slow movers — Days of Supply > 180", h2))

        n = len(slow)
        critical = int((slow["dos"] > 360).sum())
        fba_units = int(slow["fba"].fillna(0).sum())
        tied_up = slow["tied_up"].fillna(0).sum()

        E.append(Paragraph(
            f"<b>{n} SKUs</b> have more than six months of FBA stock at current sales velocity, "
            f"of which <b>{critical}</b> exceed twelve months (critical overstock). "
            f"Together they tie up roughly <b>{eur(tied_up)}</b> of inventory value "
            f"({fba_units:,} FBA units). Days of Supply = FBA Available ÷ Sales Velocity "
            f"(units/day), with both totals summed across marketplaces.", body))
        E.append(Spacer(1, 6))

        kpi = [["Slow-mover SKUs (>180 d)", "Critical (>360 d)", "FBA units locked", "Tied-up value"],
               [f"{n:,}", f"{critical:,}", f"{fba_units:,}", eur(tied_up)]]
        tk = Table(kpi, colWidths=[4.3 * cm] * 4)
        tk.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AMBER),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, 0), 8.5),
            ("FONTSIZE", (0, 1), (-1, 1), 13),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 1), (-1, 1), NAVY),
            ("BACKGROUND", (0, 1), (-1, 1), LIGHT),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 2, colors.white),
        ]))
        E.append(tk)
        E.append(Spacer(1, 12))

        top_slow = slow.head(15).copy()
        data = [["Product", "FBA", "Vel/d", "DoS", "Tied-up", "Sales", "CM3%"]]
        for _, r in top_slow.iterrows():
            data.append([
                str(r.get("Product") or r["SKU"])[:36],
                f"{int(r['fba']):,}" if pd.notna(r['fba']) else "—",
                f"{r['vel']:.1f}" if pd.notna(r['vel']) and r['vel'] > 0 else "—",
                f"{int(r['dos']):,}" if pd.notna(r['dos']) else "—",
                eur(r['tied_up']) if pd.notna(r['tied_up']) else "—",
                eur(r['Product Sales']) if pd.notna(r.get('Product Sales')) else "—",
                pct(r['CM3%']) if pd.notna(r.get('CM3%')) else "—",
            ])
        tt = Table(data, colWidths=[6.0 * cm, 1.6 * cm, 1.6 * cm, 1.6 * cm,
                                    2.1 * cm, 2.1 * cm, 1.6 * cm])
        tstyle = [
            ("BACKGROUND", (0, 0), (-1, 0), AMBER),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        # Highlight DoS > 360 in soft coral
        for i, (_, r) in enumerate(top_slow.iterrows(), start=1):
            if pd.notna(r["dos"]) and r["dos"] > 360:
                tstyle.append(("TEXTCOLOR", (3, i), (3, i), RED))
                tstyle.append(("FONTNAME", (3, i), (3, i), "Helvetica-Bold"))
        tt.setStyle(TableStyle(tstyle))
        E.append(KeepTogether([
            Paragraph("Top 15 by Days of Supply", ParagraphStyle("amb", parent=h3, textColor=AMBER)),
            tt,
        ]))
        E.append(Spacer(1, 6))
        E.append(Paragraph(
            "Tied-up value ≈ FBA units × avg unit price (sales / units over March–May), "
            "i.e. cash sitting in unsold stock. Sales / CM3% are this SKU's Mar–May economics "
            "for context. DoS values shown in red exceed 360 days (a year of stock).", small))

    E.append(Spacer(1, 8))
    E.append(Paragraph(
        "Method &amp; scope: Novadata daily margin + products exports, all marketplaces. "
        "CM3 = contribution margin after product, fulfilment and ad costs. Segments use "
        "within-period terciles; trend is a sales-weighted fit of monthly CM3%. Sub-€10k SKUs "
        "excluded from leaderboards. Days of Supply derived from FBA Available ÷ Sales Velocity.",
        small))

    doc.build(E, onFirstPage=_on_first, onLaterPages=_on_later)
    print(f"Wrote {OUT}  ({OUT.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    build()
