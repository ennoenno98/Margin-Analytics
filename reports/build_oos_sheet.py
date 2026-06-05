"""Build the OOS Impact Analytics methodology sheet (PDF) with a worked example.

Covers the full model — demand rate (lambda / Poisson), reach (days-of-supply),
the OOS vs cooling-down classification, and the EUR valuation — anchored to one
real example SKU computed from the committed data.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, Image)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "OOS_Methodology_Sheet.pdf"
CHART = ROOT / "reports" / "_oos_sheet_example.png"

# Model parameters (match oos_analytics.py defaults).
W, MIND, TAIL, OOS_DOS, CD_DOS = 90, 3.0, 21, 3.0, 30
PPC_CUT, PRICE_UP, MIN_PPC = 0.5, 0.08, 2.0
SKU, MKT = "VV-VITA-311", "amazon.it"

# ---------- compute the example SKU's daily series ----------
mp = ROOT / "novadata_exports/margin_export_2026-06-03.csv.gz"
lp = ROOT / "amazon_ledger/inventory_ledger_2026-06-03.csv.gz"
K = {"Period", "SKU", "Marketplace Name", "Units", "Product Sales",
     "Contribution Margin 3", "Advertising Costs"}
df = pd.read_csv(mp, usecols=lambda c: c in K)
df["Period"] = pd.to_datetime(df["Period"], utc=True, errors="coerce").dt.tz_localize(None)
df["CM3"] = pd.to_numeric(df["Contribution Margin 3"], errors="coerce")
for c in ("Units", "Product Sales", "Advertising Costs"):
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.rename(columns={"Product Sales": "Sales"})
df["AdSpend"] = (-df["Advertising Costs"]).clip(lower=0)

full = pd.date_range(df.Period.min(), df.Period.max(), freq="D")
asof = full.max()
d = df[(df.SKU == SKU) & (df["Marketplace Name"] == MKT)].set_index("Period")
units = d["Units"].reindex(full).fillna(0.0)
sales = d["Sales"].reindex(full).fillna(0.0)
cm3 = d["CM3"].reindex(full).fillna(0.0)
ppc = d["AdSpend"].reindex(full).fillna(0.0)
pos = units > 0
expected = (units.rolling(W, min_periods=1).sum() / units.rolling(W, min_periods=1).count()).ffill()
avg_price = (sales.rolling(W, min_periods=1).sum() / units.rolling(W, min_periods=1).sum().where(lambda x: x > 0)).ffill()
avg_cm3 = (cm3.rolling(W, min_periods=1).sum() / units.rolling(W, min_periods=1).sum().where(lambda x: x > 0)).ffill()
rps = ppc.rolling(60, min_periods=1).sum(); rpd = (ppc > 0).rolling(60, min_periods=1).sum()
base_ppc = (rps / rpd.where(rpd > 0)).ffill()
price = sales / units.where(units > 0)
had = pos.cummax(); fut = pos[::-1].cummax()[::-1]
recent = pd.Series(full >= (asof - pd.Timedelta(days=TAIL)), index=full)

# ledger EU stock + reach (SKU level)
LK = {"Date", "MSKU", "Disposition", "Ending Warehouse Balance", "Customer Shipments"}
led = pd.read_csv(lp, usecols=lambda c: c in LK)
led["Date"] = pd.to_datetime(led["Date"], format="%m/%d/%Y", errors="coerce")
for c in ("Ending Warehouse Balance", "Customer Shipments"):
    led[c] = pd.to_numeric(led[c], errors="coerce").fillna(0)
led = led[(led.Disposition == "SELLABLE") & (led.MSKU == SKU)]
eu = led.groupby("Date").agg(eu_stock=("Ending Warehouse Balance", "sum"),
                             shp=("Customer Shipments", "sum"))
eu["shipped"] = (-eu["shp"]).clip(lower=0)
eu_stock = eu["eu_stock"].reindex(full).ffill()
avgship = eu["shipped"].reindex(full).rolling(28, min_periods=5).mean()
dos = eu_stock / avgship.where(avgship > 0)

# classify
phys = (eu_stock <= 0) & had
low_reach = (dos < OOS_DOS) & had
ad_cut = (base_ppc > MIN_PPC) & (ppc <= base_ppc * (1 - PPC_CUT))
hike = price.notna() & (price >= avg_price * (1 + PRICE_UP))
cool_stock = (dos >= OOS_DOS) & (dos < CD_DOS)
cooldown = (ad_cut | hike) & cool_stock & (units > 0) & (expected >= MIND) & had & ~phys
oos_gap = (units == 0) & (expected >= MIND) & had & (fut | recent)
oos = (phys | low_reach | oos_gap) & ~cooldown
cat = np.where(phys, "Physical OOS",
      np.where(low_reach, "Critically low (<3d)",
      np.where(cooldown, "Cooling down",
      np.where(oos, "Marketplace gap", "—"))))
lost_u = np.where(oos, np.clip(expected - units, 0, None), 0.0)
miss_u = np.where(cooldown, np.clip(expected - units, 0, None), 0.0)
panel = pd.DataFrame({
    "units": units, "exp": expected, "eu_stock": eu_stock, "dos": dos,
    "price": price, "label": cat,
    "lost_rev": lost_u * avg_price.fillna(0), "lost_cm3": lost_u * avg_cm3.fillna(0),
    "miss_rev": miss_u * avg_price.fillna(0), "miss_cm3": miss_u * avg_cm3.fillna(0),
}, index=full)

# annual summary (this SKU x marketplace)
summ = dict(
    oos_days=int((panel["label"].isin(["Physical OOS", "Critically low (<3d)", "Marketplace gap"])).sum()),
    cool_days=int((panel["label"] == "Cooling down").sum()),
    lost_rev=panel.lost_rev.sum(), lost_cm3=panel.lost_cm3.sum(),
    miss_rev=panel.miss_rev.sum(), miss_cm3=panel.miss_cm3.sum(),
)
title_en = pd.read_csv(ROOT / "product_titles_en.csv", dtype=str).set_index("SKU")["Title"].get(SKU, SKU)

# ---------- chart: auto-pick the ~75-day window that best shows BOTH ----------
OOS_LBLS = ["Physical OOS", "Critically low (<3d)", "Marketplace gap"]
is_oos = panel["label"].isin(OOS_LBLS).astype(int)
is_cd = (panel["label"] == "Cooling down").astype(int)
ro = is_oos.rolling(75, min_periods=1).sum()
rc = is_cd.rolling(75, min_periods=1).sum()
score = pd.concat([ro, rc], axis=1).min(axis=1)   # window with both present
w_end = score.idxmax(); w_start = w_end - pd.Timedelta(days=74)
win = pd.date_range(w_start, w_end)
p = panel.reindex(win)
w_oos = int(p["label"].isin(["Physical OOS", "Critically low (<3d)", "Marketplace gap"]).sum())
w_cd = int((p["label"] == "Cooling down").sum())
w_hi, w_lo, w_reach = p.eu_stock.max(), p.eu_stock.min(), p.dos.min()
cdw = p[p["label"] == "Cooling down"]
cd_price = cdw["price"].median() if len(cdw) else float("nan")
cd_units = cdw["units"].median() if len(cdw) else 0.0
base_pr = avg_price.reindex(win).median()
lam_w = p["exp"].median()
# price + ad spend just BEFORE the throttle vs DURING it
cd0 = cdw.index.min() if len(cdw) else win[0]
pre = pd.date_range(cd0 - pd.Timedelta(days=30), cd0 - pd.Timedelta(days=1))
pre_price = price.reindex(pre).dropna().median()
pre_ppc = ppc.reindex(pre).mean()
cd_ppc = ppc.reindex(cdw.index).mean() if len(cdw) else 0.0
fig, ax = plt.subplots(figsize=(9, 3.4))
ax.bar(win, p.units.values, color="#2a9d8f", label="Units sold/day")
ax.plot(win, p["exp"].values, color="#264653", lw=1.6, ls=":", label="Expected demand (lambda)")
shade = {"Physical OOS": "#b00020", "Critically low (<3d)": "#d32f2f",
         "Marketplace gap": "#f4a261", "Cooling down": "#9b5de5"}
for c, col in shade.items():
    for x in win[p["label"].values == c]:
        ax.axvspan(x - pd.Timedelta(hours=12), x + pd.Timedelta(hours=12), color=col, alpha=0.18, lw=0)
ax2 = ax.twinx()
ax2.plot(win, p.eu_stock.values, color="#8d99ae", lw=1.6, label="EU stock (ledger)")
ax.set_ylabel("Units / day"); ax2.set_ylabel("EU sellable stock")
ax.set_title(f"{SKU} ({title_en}) — {MKT}: {w_start:%d %b} – {w_end:%d %b %Y}")
h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
ax.legend(h1 + h2, l1 + l2, loc="upper center", fontsize=7.5, ncol=3)
fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(CHART, dpi=150); plt.close(fig)

# representative rows: ensure both cooling-down and OOS days are shown
exw = panel.reindex(win)
ex = pd.concat([
    exw[exw["label"] == "Cooling down"].head(8),
    exw[exw["label"].isin(OOS_LBLS)].head(10),
    exw[exw["label"] == "—"].iloc[::15].head(4),
]).drop_duplicates().sort_index()
rows = [["Date", "Units", "λ", "EU stk", "Reach", "Price", "Category", "Lost €", "Miss €"]]
for dt, r in ex.head(15).iterrows():
    rows.append([dt.strftime("%m-%d"), f"{r.units:.0f}", f"{r['exp']:.0f}",
                 f"{r.eu_stock:.0f}", ("%.0f" % r.dos) if pd.notna(r.dos) else "–",
                 ("%.2f" % r.price) if pd.notna(r.price) else "–", r["label"],
                 f"{r.lost_cm3:.0f}" if r.lost_cm3 else "", f"{r.miss_cm3:.0f}" if r.miss_cm3 else ""])

# ---------- PDF ----------
def eu_(x): return f"{x:,.0f}".replace(",", ".")
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], textColor=colors.HexColor("#264653"), fontSize=16)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=colors.HexColor("#2a9d8f"), fontSize=11.5)
body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9.3, leading=12.5)
small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=7.8, leading=9.5, textColor=colors.grey)
doc = SimpleDocTemplate(str(OUT), pagesize=A4, topMargin=1.4*cm, bottomMargin=1.2*cm,
                        leftMargin=1.7*cm, rightMargin=1.7*cm)
E = []
def P(t, st=body): E.append(Paragraph(t, st))
def gap(h=0.25): E.append(Spacer(1, h*cm))

def mktable(rows, col_w=None, header=True, fs=8.2, hl=None):
    t = Table(rows, colWidths=col_w, repeatRows=1 if header else 0)
    sty = [("FONTSIZE", (0, 0), (-1, -1), fs),
           ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
           ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
    if header:
        sty += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#264653")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(sty)); return t

P("OOS Impact Analytics — Methodology Sheet", H1)
P("How the dashboard detects out-of-stock and demand-throttling events and "
  "values their revenue / contribution-margin impact. Worked example: "
  f"<b>{SKU} — {title_en}</b> ({MKT}). Prepared for review.", small)
gap()

P("1. Data sources", H2)
P("• <b>Amazon FBA Inventory Ledger</b> — real daily sellable warehouse balance "
  "and units shipped per SKU. The account runs <b>Pan-EU</b>, so stock is pooled: "
  "a SKU is physically out only when the whole EU balance hits zero.<br/>"
  "• <b>Novadata margin export</b> — daily units, sales and contribution margin "
  "(CM3) per SKU and marketplace; supplies price and CM3 per unit for valuation.<br/>"
  "• <b>Shopify catalog</b> — English product titles.")
gap()

P("2. Two core measures", H2)
P("<b>Demand rate (lambda).</b> Trailing 90-day average units per calendar day, "
  "per SKU and marketplace. Daily sales follow an approximate Poisson process, so "
  "the chance of selling zero on a normal day is P(0)=e^(-lambda): ~37% at "
  "1/day, ~5% at 3/day, ~0.7% at 5/day. A zero-sales day is therefore only "
  "treated as a stock-out when lambda is high enough that zero is a real anomaly "
  "(default threshold 3/day).<br/>"
  "<b>Reach (days-of-supply).</b> EU sellable stock divided by trailing 28-day "
  "average units shipped — how many days of cover remain.")
gap()

P("3. Daily classification (one label per SKU x marketplace x day)", H2)
P("Evaluated in priority order; the first match wins:")
E.append(mktable([
    ["Priority", "Label", "Condition", "Counts as"],
    ["1", "Physical OOS", "EU sellable balance = 0", "Lost (involuntary)"],
    ["2", "Critically low", "Reach < 3 days", "Lost (involuntary)"],
    ["3", "Cooling down", "3 <= reach < 30 days, still selling (units > 0),\nthrottled (price >= +8% and/or ad cut >= 50%)", "Miss (voluntary)"],
    ["4", "Marketplace gap", "Units = 0 on a day enclosed by sales,\nlambda >= 3/day (incl. throttles that hit 0)", "Lost (involuntary)"],
], col_w=[1.4*cm, 3.0*cm, 7.7*cm, 3.4*cm], fs=8))
P("Guards: the SKU must already be selling (ignores pre-launch); a zero-run must "
  "be enclosed by later sales or be within the last 21 days (ignores discontinued "
  "tails). Cooling-down separates deliberate throttling — which otherwise looks "
  "like a demand drop — from genuine stock-outs, so the two are never "
  "double-counted.", small)
gap()

P("4. Impact valuation", H2)
P("On any flagged day: <b>missed units = max(lambda - actual units, 0)</b> "
  "(the expected demand we didn't capture). Valued at the SKU's trailing average "
  "selling price and CM3 per unit:<br/>"
  "&nbsp;&nbsp;<b>Lost / Miss revenue = missed units x avg price</b><br/>"
  "&nbsp;&nbsp;<b>Lost / Miss CM3 = missed units x avg CM3 per unit</b> "
  "(the true P&amp;L impact).<br/>"
  "'Lost' = involuntary (OOS); 'Miss' = voluntary (cooling down). Consecutive "
  "flagged days are grouped into discrete events.")
gap()

P("5. Worked example", H2)
P(f"<b>{SKU} — {title_en}</b>, {MKT}. Full year on this marketplace: "
  f"<b>{summ['oos_days']} OOS days</b> (lost €{eu_(summ['lost_rev'])} revenue / "
  f"€{eu_(summ['lost_cm3'])} CM3) and <b>{summ['cool_days']} cooling-down days</b> "
  f"(miss €{eu_(summ['miss_rev'])} revenue / €{eu_(summ['miss_cm3'])} CM3).")
E.append(Image(str(CHART), width=16.5*cm, height=6.2*cm))
gap(0.15)
P(f"Highlighted window {w_start:%d %b %Y} – {w_end:%d %b %Y}: EU stock ranged "
  f"from ~{eu_(w_hi)} down to ~{eu_(w_lo)} units and reach bottomed at "
  f"~{w_reach:.0f} day(s). In this window the model flagged <b>{w_oos} OOS day(s)</b> "
  f"(red/orange shading) and <b>{w_cd} cooling-down day(s)</b> (purple) — the SKU "
  "was throttled (price up / ad spend cut) while stock was tight, then went into "
  "genuine stock-out as reach collapsed. Note the balance rarely hits literally "
  "zero, so the <b>reach &lt; 3</b> rule, not a balance-of-zero rule, is what "
  "catches the hard stock-out.")
gap(0.15)
P("<b>What \"cooling down\" means here.</b> On the purple days the SKU kept "
  "selling (units &gt; 0) but demand was deliberately damped to stretch the "
  f"remaining stock to the next shipment. <b>Before cooling down</b> it sold at "
  f"~€{pre_price:.2f} with ~€{eu_(pre_ppc)}/day of ad spend; <b>during the "
  f"throttle</b> the price was lifted to ~€{cd_price:.2f} and ad spend cut to "
  f"~€{eu_(cd_ppc)}/day. Sales fell to about {cd_units:.0f}/day versus the "
  f"expected ~{lam_w:.0f}/day, and that gap is booked as a <b>voluntary miss</b> "
  "— a pricing / advertising choice to protect availability and the listing's "
  "ranking — not a lost stock-out. Crucially the SKU keeps selling: the moment a "
  "throttle pushes sales to zero it is no longer cooling down and counts as OOS "
  "(lost). Cooling down trades a little volume now (the miss) to avoid the larger "
  "ranking and lost-sales hit of a full stock-out.")
gap(0.15)
P("Representative days (Lost/Miss shown as CM3 €):", small)
E.append(mktable(rows, col_w=[1.5*cm, 1.2*cm, 1.0*cm, 1.4*cm, 1.3*cm, 1.4*cm,
                              4.0*cm, 1.5*cm, 1.5*cm], fs=7.6))
gap()

P("6. Caveats", H2)
P("Impact figures are estimates (assume the SKU would have sold at its recent "
  "rate). Ad-spend-cut detection only applies from when Novadata began reporting "
  "Advertising Costs (~Feb 2026); the price-hike lever spans the full year. Reach "
  "is EU-pooled, so reach-based rules need ledger coverage for the SKU. All "
  "thresholds (3/day, reach 3 & 30, +8%, -50%) are tunable in the dashboard.", small)

doc.build(E)
print("WROTE", OUT, OUT.stat().st_size, "bytes |", summ)
