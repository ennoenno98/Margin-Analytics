"""Render the marketing-team OOS dashboard user guide to a styled PDF."""
from __future__ import annotations
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, ListFlowable, ListItem)

OUT = Path(__file__).resolve().parent / "OOS_Dashboard_User_Guide.pdf"
DARK = colors.HexColor("#264653")
TEAL = colors.HexColor("#2a9d8f")
GREY = colors.HexColor("#cccccc")
LIGHT = colors.HexColor("#eef4f3")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], textColor=DARK, fontSize=18, spaceAfter=2)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=TEAL, fontSize=12.5,
                    spaceBefore=10, spaceAfter=4)
body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9.3, leading=13, alignment=TA_LEFT)
small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=8, leading=10.5, textColor=colors.grey)
cellh = ParagraphStyle("cellh", parent=body, fontSize=8.8, leading=11, textColor=colors.white,
                       fontName="Helvetica-Bold")
cell = ParagraphStyle("cell", parent=body, fontSize=8.6, leading=11)
callout = ParagraphStyle("callout", parent=body, fontSize=9.3, leading=13,
                         backColor=LIGHT, borderColor=TEAL, borderWidth=0,
                         leftIndent=6, rightIndent=6, spaceBefore=2, spaceAfter=2,
                         borderPadding=6)

E = []
def P(t, st=body): E.append(Paragraph(t, st))
def gap(h=0.18): E.append(Spacer(1, h * cm))
def bullets(items, st=body):
    E.append(ListFlowable(
        [ListItem(Paragraph(t, st), leftIndent=10, value="•") for t in items],
        bulletType="bullet", start="•", leftIndent=12, bulletFontSize=7))

def table(rows, widths):
    data = [[Paragraph(c, cellh if i == 0 else cell) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1)
    sty = [("BACKGROUND", (0, 0), (-1, 0), DARK),
           ("GRID", (0, 0), (-1, -1), 0.4, GREY),
           ("VALIGN", (0, 0), (-1, -1), "TOP"),
           ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
           ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT])]
    t.setStyle(TableStyle(sty))
    E.append(t)

# ---------------------------------------------------------------- content
P("OOS Impact Analytics — User Guide", H1)
P("A quick guide for the marketing team. No technical knowledge needed.", small)
gap()

P("What this dashboard is for", H2)
P("It answers one question: <b>how much revenue and margin are we losing because "
  "products aren't available on Amazon — and which products are worst?</b> Every "
  "day it compares what each product <i>actually</i> sold against what it <i>would</i> "
  "have sold if it had been in stock, and turns the gap into euros. Use it to spot "
  "which SKUs to prioritise for restocking, which listings need attention, and how "
  "much a stock-out really cost.")
gap(0.1)
P("<b>One rule to remember:</b> the headline number is <b>Lost CM3</b> — the "
  "contribution margin (real profit) we gave up. <b>Lost revenue</b> is the top-line "
  "version of the same thing.", callout)
gap()

P("Getting in", H2)
P("Open the dashboard link and enter the shared password. That's it — the data "
  "refreshes automatically, so you always see the latest.")
gap()

P("The filter bar (top of the page)", H2)
P("Five controls sit across the top. Set these first; everything below updates "
  "instantly.")
gap(0.08)
table([
    ["Filter", "What it does"],
    ["Region", "<b>EU</b> (all European marketplaces, pooled), <b>GB</b> (UK / "
               "amazon.co.uk), or <b>EU + UK (combined)</b> for the total. EU and UK "
               "stock are separate warehouses, so they're tracked apart by default."],
    ["Period", "Full range, or drill into a Year / Quarter / Month / Week. You can "
               "pick more than one (e.g. two months)."],
    ["Min demand", "How busy a product must be before a no-sales day counts as a "
                   "stock-out. Leave at the default (3/day) unless investigating slow movers."],
    ["Search", "Type part of a product name or SKU to focus on one product or a range."],
], [3.0 * cm, 13.2 * cm])
gap(0.1)
P("There's also a <b>thresholds</b> expander. You can ignore it — the defaults are "
  "set with Ops. Open it only to tune sensitivity (e.g. how tight stock must be to "
  "count as “critically low”).")
gap()

P("The headline (under the filters)", H2)
bullets([
    "A <b>chart</b> of lost revenue, lost CM3, and how many SKUs were out of stock, over time.",
    "<b>Lost</b> — sales we <i>couldn't</i> make because we were out of stock (the real problem).",
    "<b>Miss</b> (Cooling down) — sales we <i>chose</i> to give up by easing off (raising price / "
    "cutting ads) to stretch tight stock. Voluntary, so kept separate.",
    "<b>Ramp-up</b> (Heating up) — the cost of getting a product going again after it came back.",
    "<b>Listing blocked</b> — products on healthy stock but <b>not selling at all</b>: the "
    "listing may be suppressed. Shown as “unrealized” and <b>not</b> counted as a stock-out. "
    "<b>Check these listings.</b>",
    "<b>OOS rate</b> — share of time out of stock. <b>WISR</b> — in-stock rate weighted by how "
    "important each product is (higher is better).",
])
gap()

P("The tabs — where to look for what", H2)
table([
    ["Tab", "Use it to…"],
    ["Most affected SKUs", "See the ranked list of biggest losses. Click a SKU for its "
                           "stock &amp; sales timeline. <b>Start here.</b>"],
    ["Stock-out calendar", "A month-by-month heatmap — spot seasonal or recurring patterns."],
    ["Stock-out events", "Every stock-out as a row (start, end, duration, cause, € lost). "
                         "Download as CSV."],
    ["Cooling down", "Products we deliberately throttled to protect tight stock — and the cost."],
    ["Heating up", "Products recovering after coming back — extra ad spend + sales still ramping."],
    ["Country overview", "Which countries carry the loss, valued at each country's own price "
                         "and margin. Drill into one country."],
    ["Top sellers", "A watchlist of the highest-value products and their current stock status."],
], [3.4 * cm, 12.8 * cm])
gap(0.1)
P("Most tables have a <b>download button</b> for exporting to Excel / CSV.")
gap()

P("Reading a “cause” label", H2)
bullets([
    "<b>Physical (network)</b> — genuinely zero stock in the warehouse.",
    "<b>Critically low (reach)</b> — almost out; days of stock left below the safe threshold.",
    "<b>Demand gap</b> — in stock on paper but selling nothing when it should be — usually a "
    "real availability problem.",
    "<b>Suppressed sales (post-OOS)</b> — still recovering after a stock-out.",
    "<b>Cooling down</b> — we eased off on purpose (not a problem).",
    "<b>Listing blocked (in stock)</b> — plenty of stock, zero sales → <b>a listing issue to "
    "investigate</b>, not a stock-out.",
    "<b>Heating up</b> — being pushed back to full speed after returning.",
])
gap()

P("A typical workflow", H2)
E.append(ListFlowable([
    ListItem(Paragraph(t, body), leftIndent=12) for t in [
        "Pick your <b>Region</b> and <b>Period</b> (e.g. EU, last quarter).",
        "Look at the <b>headline KPIs</b> — how much did we lose, and is the trend up or down?",
        "Open <b>Most affected SKUs</b> and note the top few by Lost CM3.",
        "Click one to see <i>when</i> and <i>why</i> it went out — real stock-out, deliberate "
        "throttle, or blocked listing?",
        "Check the <b>Listing blocked</b> list — quick wins (a listing fix, no restock).",
        "Export <b>Stock-out events</b> or the ranked table to share with Ops / Supply Chain.",
    ]], bulletType="1", leftIndent=14))
gap()

P("Good to know", H2)
bullets([
    "<b>Numbers are estimates, not the accounting P&amp;L.</b> They measure lost <i>opportunity</i> "
    "from Amazon's own stock and sales data, so they won't tie to the cent with finance. For "
    "prioritising, not for booking.",
    "<b>EU vs UK are separate warehouses</b> — that's why they're split. Use <b>combined</b> for "
    "the total, but restocking decisions are per-region.",
    "<b>The newest day is always one day behind</b> — today's data is partial, so it's excluded "
    "until complete.",
    "Everything is in <b>euros, whole numbers, European formatting</b> (1.234,5), with <b>English "
    "product names</b>.",
])
gap(0.2)
P("Questions or a metric that looks off? Flag it — the model's thresholds are all tunable and "
  "documented in the methodology sheet.", small)

SimpleDocTemplate(str(OUT), pagesize=A4, topMargin=1.4 * cm, bottomMargin=1.2 * cm,
                  leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                  title="OOS Impact Analytics — User Guide").build(E)
print("WROTE", OUT, OUT.stat().st_size, "bytes")
