"""Build a shareable PDF explaining the OOS lambda/Poisson methodology
with the VV-VITA-208 Nov-Dec 2025 worked example."""
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
SKU = "VV-VITA-208"
OUT = ROOT / "reports" / "OOS_Methodology_Review.pdf"
CHART = ROOT / "reports" / "_oos_example.png"

# ---------- data ----------
nd = pd.read_csv(ROOT/"novadata_exports/margin_export_2026-06-03.csv.gz",
                 usecols=lambda c: c in {"Period","SKU","Marketplace Name","Units"})
nd["Period"] = pd.to_datetime(nd["Period"], utc=True, errors="coerce").dt.tz_localize(None)
s = nd[nd.SKU == SKU]
permkt = s.groupby("Marketplace Name", observed=True).Units.sum().sort_values(ascending=False)
days = s.Period.nunique()
lam = (permkt/365).rename("lam")
mkt_rows = [["Marketplace","Units / yr","lambda (units/day)","P(0)=e^(-lambda)","Flag @ 3/day?"]]
for mk in ["amazon.de","amazon.co.uk","amazon.es","amazon.fr","amazon.it"]:
    if mk in lam.index:
        L = lam[mk]; p0 = np.exp(-L)
        mkt_rows.append([mk, f"{permkt[mk]:,.0f}", f"{L:,.1f}", f"{p0*100:,.1f}%",
                         "Yes" if L>=3 else "No"])

de = s[s["Marketplace Name"]=="amazon.de"].set_index("Period").Units.sort_index()
de_m = de.resample("MS").agg(["sum","count"])
de_m["lam"] = de_m["sum"]/de_m["count"]

# ---------- ledger for chart ----------
LK = {"Date","MSKU","Disposition","Ending Warehouse Balance","Customer Shipments"}
led = pd.read_csv(ROOT/"amazon_ledger/inventory_ledger_2026-06-03.csv.gz",
                  usecols=lambda c: c in LK)
led["Date"] = pd.to_datetime(led["Date"], format="%m/%d/%Y", errors="coerce")
led["Ending Warehouse Balance"] = pd.to_numeric(led["Ending Warehouse Balance"], errors="coerce").fillna(0)
led = led[(led.MSKU==SKU)&(led.Disposition=="SELLABLE")]
eu = led.groupby("Date")["Ending Warehouse Balance"].sum()
win = pd.date_range("2025-11-01","2025-12-31")
eu = eu.reindex(win).ffill()
de_w = de.reindex(win).fillna(0)

fig, ax = plt.subplots(figsize=(9,3.6))
ax.bar(win, de_w.values, color="#2a9d8f", label="DE units sold/day")
zero = win[de_w.values==0]
for z in zero:
    ax.axvspan(z-pd.Timedelta(hours=12), z+pd.Timedelta(hours=12), color="#d32f2f", alpha=0.12)
ax2 = ax.twinx()
ax2.plot(win, eu.values, color="#264653", lw=1.8, label="EU sellable stock")
ax2.axhline(0, color="grey", lw=0.6)
ax.set_ylabel("DE units sold / day"); ax2.set_ylabel("EU sellable stock (units)")
ax.set_title(f"{SKU} — Nov–Dec 2025: stock draw-down vs sales (zero-sales days shaded)")
l1,lab1 = ax.get_legend_handles_labels(); l2,lab2 = ax2.get_legend_handles_labels()
ax.legend(l1+l2, lab1+lab2, loc="upper center", fontsize=8, ncol=2)
fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(CHART, dpi=150); plt.close(fig)

# ---------- PDF ----------
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], textColor=colors.HexColor("#264653"))
H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=colors.HexColor("#2a9d8f"))
body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9.5, leading=13)
small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=8, leading=10, textColor=colors.grey)
doc = SimpleDocTemplate(str(OUT), pagesize=A4, topMargin=1.6*cm, bottomMargin=1.4*cm,
                        leftMargin=1.8*cm, rightMargin=1.8*cm)
E = []
def P(t,st=body): E.append(Paragraph(t,st))
def gap(h=0.3): E.append(Spacer(1,h*cm))

def mktable(rows, col_w=None, header=True):
    t = Table(rows, colWidths=col_w)
    sty = [("FONTSIZE",(0,0),(-1,-1),8.5),
           ("GRID",(0,0),(-1,-1),0.4,colors.HexColor("#cccccc")),
           ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
           ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
           ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]
    if header:
        sty += [("BACKGROUND",(0,0),(-1,0),colors.HexColor("#264653")),
                ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold")]
    t.setStyle(TableStyle(sty)); return t

P("OOS Impact Analytics — Stock-out Detection Methodology", H1)
P("Lambda / Poisson logic and a worked example (VV-VITA-208). Prepared for review.", small)
gap()
P("1. The question", H2)
P("For each SKU and marketplace we observe daily units sold. A day with <b>zero "
  "sales</b> may be a genuine stock-out (lost sales) or just a normal quiet day. "
  "We need a rule that tells these apart without manual review, then values the "
  "lost units in EUR (revenue and contribution margin).", )
gap()
P("2. The lambda / Poisson rule", H2)
P("Let <b>lambda</b> be a SKU's expected demand rate = its trailing 90-day average "
  "units per calendar day (per marketplace, forward-filled). Sales arriving "
  "through a day behave approximately like a Poisson process.", )
gap(0.15)
P("<b>Background — the Poisson distribution.</b> For a process expecting lambda "
  "events in a given interval, the probability of exactly k events in that "
  "interval is:", )
P("P(k) = ( lambda^k &times; e^(-lambda) ) / k!", ParagraphStyle("f",parent=body,alignment=1,fontSize=11))
P("Classic uses include the number of calls received at a call centre, or "
  "radioactive-decay events in a fixed observation period. For instance, a call "
  "centre averaging lambda = 3 calls per minute receives 1 to 4 calls in any given "
  "minute with probability about 0.77, while 0 or at least 5 calls has probability "
  "about 0.23. Our SKUs are directly analogous: units sold per day in place of "
  "calls per minute.", )
gap(0.1)
P("Setting k = 0 (a zero-sales day), the formula reduces to what we use:", )
P("<b>P(0 sales) = e^(-lambda)</b>", ParagraphStyle("f",parent=body,alignment=1,fontSize=11))
P("So the higher a SKU's demand rate, the less plausible a zero-sales day is by "
  "chance — and the stronger the signal that something blocked availability.", )
gap(0.2)
P("We flag a zero-sales day as a (marketplace) stock-out when lambda clears a "
  "threshold (default <b>3 units/day</b>), in addition to two guards: the SKU must "
  "be an established seller (sold before) and the zero-run must be enclosed by "
  "later sales or be ongoing in the last 21 days (so we ignore pre-launch and "
  "discontinued products). Physical stock-outs (EU sellable balance = 0 in the "
  "Amazon ledger) are always flagged regardless.", )
gap(0.2)
P("How the threshold maps to false-positive rate:", small)
E.append(mktable([
    ["lambda (units/day)","P(0)=e^(-lambda)","Reading"],
    ["1","~37%","zeros are normal -> ignore"],
    ["2","~14%","still common"],
    ["3 (default)","~5%","zero is anomalous -> flag"],
    ["5","~0.7%","near-certain block"],
    ["30","~0%","unmistakable"],
], col_w=[4*cm,4*cm,8*cm]))
gap(0.15)
P("Why 3 as the default: it is a reasoned balance, not a proven optimum. "
  "P(0)=e^(-3) is about 5%, the conventional ~2-sigma significance cutoff, so a "
  "zero day below ~5% natural likelihood counts as a real anomaly. Below 3 the "
  "false-positive rate climbs steeply (14% at 2, 37% at 1) and the slow-moving "
  "long tail floods the results; much above 3 we start missing genuine stock-outs "
  "of moderate-velocity SKUs. On the live data 3/day cleanly excluded thin-market "
  "noise (FR/IT at ~1/day) while catching the real DE/UK events, and produced a "
  "sensible total (about EUR 238k lost CM3/yr; median ~10 OOS days per affected "
  "SKU). It is exposed as a slider so reviewers can tune sensitivity; a per-SKU "
  "k-sigma rule (flag when lambda >= k x std) is the natural next step.", )
gap()
P("3. Worked example — VV-VITA-208 across marketplaces", H2)
P("Same SKU, very different verdicts, driven entirely by lambda (approx. yearly "
  "average units/day):", )
E.append(mktable(mkt_rows, col_w=[3.6*cm,2.6*cm,3.4*cm,3.2*cm,3*cm]))
gap(0.15)
P("In Germany a zero-sales day is essentially impossible by chance, so it is a "
  "true stock-out; in France/Italy (~1/day) zeros happen ~1 day in 3 and carry no "
  "signal. This is why the model flagged this SKU in DE and UK but not FR/IT/ES.", )
E.append(Spacer(1,0.4*cm))
P("4. The real Nov-Dec 2025 stock-out (from the Amazon ledger)", H2)
E.append(Image(str(CHART), width=17*cm, height=6.8*cm))
gap(0.15)
P("EU network stock fell from 814 to about 30 units across November for a SKU "
  "selling 30-50/day in DE; no replenishment arrived until 16 Dec (a 3,087-unit "
  "receipt). As stock ran critically low, DE sales flatlined for 12 days (red). "
  "Crucially the EU balance never hit zero (floor ~23), so a pure 'balance = 0' "
  "rule would have caught none of these days — the lambda/Poisson marketplace "
  "signal is what detects this low-stock throttle. Estimated lost units on each "
  "flagged day = lambda (the current demand rate), valued at the SKU's trailing "
  "average price and CM3 per unit.", )
gap(0.2)
P("Caveat: Novadata sales and Amazon ledger shipments do not align perfectly "
  "day-to-day (order vs fulfilment dating), and real sales are somewhat more "
  "variable than pure Poisson (weekends, promotions). The percentages above are "
  "decision intuition, not exact guarantees; the threshold is tunable.", small)

doc.build(E)
print("WROTE", OUT, OUT.stat().st_size, "bytes")
