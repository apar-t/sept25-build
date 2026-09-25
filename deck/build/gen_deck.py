"""Night's Watch demo deck: 4 slides, 3-minute slot, most of it on the live prototype.

    bash deck/build/build-deck.sh      # writes deck/nights-watch.html and deck/nights-watch.pdf

Style: landing/v3.html ("Night"): #050913 ground, ice #9fd8ff accent, red #ff6767 for alerts, Geist + Geist Mono.
Numbers on slide 4 come from `uv run python -m sept25_build.agent.proof` on origin/main b5455aa
(365-day sim, seed 7, tokens estimated len//4). build/series.json is the same run, one row per day.

Passes
  1  2026-09-25  first build: cover, problem, how it works, a year simulated
  2  2026-09-25  slide 3 'Live demo': setup and the three injections from the preflight cue card (5 slides)
  3  2026-09-25  cover: logo moved left of the title, on the same line
  4  2026-09-25  cover: logo about 20% bigger
  5  2026-09-25  slide 2 point 2: 'notice' reworded to data policy + the only sign is a page edit
"""

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECK = HERE.parent
OUT = DECK / "nights-watch.html"

ICE, RED, INK, INK2, INK3 = "#9fd8ff", "#ff6767", "#eef3fb", "#9ba9c0", "#4f5c74"
NAIVE = "#7d8aa3"
LINE, LINE2 = "rgba(159,216,255,.1)", "rgba(159,216,255,.18)"
TOWER = "M5 4h3v3h2V4h4v3h2V4h3v6H5zM7 11h10v11H7z"

CSS = """
@page{size:13.333in 7.5in;margin:0}
:root{--bg:#050913;--ink:#eef3fb;--ink-2:#9ba9c0;--ink-3:#4f5c74;--line:rgba(159,216,255,.1);--line-2:rgba(159,216,255,.18);
  --ice:#9fd8ff;--red:#ff6767;--red-bg:rgba(255,103,103,.1);
  --sans:Geist,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --mono:"Geist Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);-webkit-font-smoothing:antialiased}
.slide{width:13.333in;height:7.5in;position:relative;overflow:hidden;break-after:page;page-break-after:always;
  padding:.6in .8in .55in;display:flex;flex-direction:column;
  background:radial-gradient(ellipse 70% 55% at 88% 118%,rgba(95,180,255,.11),transparent 70%),var(--bg)}
.slide:last-child{break-after:auto;page-break-after:auto}
h1{font:600 30pt/1.1 var(--sans);letter-spacing:-.03em;color:var(--ice);margin:0 0 .2in}
.rule{height:1px;background:var(--line-2);margin:0 0 .5in}
.body{flex:1;min-height:0;display:grid;grid-template-columns:4.35in 1fr;gap:.6in;align-items:center}
.pts{list-style:none;margin:0;padding:0}
.pts li{position:relative;padding-left:.3in;font:400 16.5pt/1.42 var(--sans);color:var(--ink-2);margin:0 0 .3in;letter-spacing:-.005em}
.pts li:last-child{margin-bottom:0}
.pts li:before{content:"";position:absolute;left:0;top:.6em;width:7px;height:7px;border-radius:50%;background:var(--ice);box-shadow:0 0 8px var(--ice)}
.pts b{color:var(--ink);font-weight:600}
.nb{white-space:nowrap}
.fig{display:flex;flex-direction:column;justify-content:center;min-width:0}
.cav{font:400 12pt/1.4 var(--sans);color:var(--ink-2);margin:.2in 0 0}
svg text{font-family:var(--sans)}
svg .m{font-family:var(--mono)}

/* cover */
.cover{justify-content:center;padding:0 1.1in;
  background:radial-gradient(ellipse 80% 55% at 70% 115%,rgba(95,180,255,.18),transparent 70%),linear-gradient(180deg,#03060e,var(--bg) 60%)}
.cover .stars{position:absolute;inset:0;width:100%;height:100%}
.cover .in{position:relative}
.cover .tl{display:flex;align-items:center;gap:.26in}
.cover .mark{width:59px;height:76px;flex:0 0 59px;color:var(--ice);display:block;filter:drop-shadow(0 0 12px rgba(159,216,255,.5))}
.cover h1{font:600 66pt/1 var(--sans);letter-spacing:-.045em;color:var(--ink);margin:0}
.cover .agenda{font:500 21pt/1.3 var(--sans);color:var(--ice);margin:.42in 0 0;letter-spacing:-.015em}
.cover .who{position:absolute;left:1.1in;bottom:.65in;font:400 12pt/1.7 var(--mono);color:var(--ink-2);letter-spacing:.02em}

/* problem: the page diff */
.diff{border:1px solid var(--line-2);border-radius:16px;background:rgba(14,22,40,.6);overflow:hidden;font:13.5pt var(--mono);
  box-shadow:0 30px 80px -30px rgba(0,0,0,.8)}
.diff .hd{display:flex;justify-content:space-between;gap:12px;padding:.17in .24in;border-bottom:1px solid var(--line);color:var(--ink-3);font-size:11.5pt}
.diff .hd b{color:var(--red);font-weight:400}
.diff .r{display:flex;justify-content:space-between;gap:12px;padding:.16in .24in;border-bottom:1px solid var(--line);color:var(--ink-2)}
.diff .r:last-child{border-bottom:0}
.diff .r span:last-child{color:var(--ink-3)}
.diff .r.add{background:var(--red-bg);color:var(--ink);box-shadow:inset 3px 0 0 var(--red)}
.diff .r.add span:first-child:before{content:"+ ";color:var(--red)}
.diff .r.add span:last-child{color:var(--red)}

/* demo: the three injections */
.steps .r{align-items:center;padding:.22in .24in}
.steps .r>b{font:500 12pt var(--mono);color:var(--ice);flex:0 0 .34in}
.steps .r>span:nth-child(2){flex:1;color:var(--ink);font:400 14pt/1.35 var(--sans)}
.diff.steps .hd b{color:var(--ink-3)}
.diff.steps .r span.out{font:500 12pt var(--mono);white-space:nowrap;padding:.05in .13in;border-radius:999px;border:1px solid var(--line-2);color:var(--ink-2)}
.diff.steps .r span.out.bad{border-color:rgba(255,103,103,.55);color:var(--red);background:var(--red-bg)}
"""


def seed(i: float) -> float:
    x = math.sin(i * 127.1) * 43758.5
    return x - math.floor(x)


def stars(w: int = 1280, h: int = 720, n: int = 170) -> str:
    dots = []
    for i in range(n):
        x, y = seed(i + 1) * w, seed(i + 7.3) * h
        a = (.12 + seed(i + 5.1) * .55) * max(.12, 1 - y / 820)
        r = 1.2 if seed(i + 3.7) < .08 else .7
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="rgb(210,230,255)" fill-opacity="{a:.2f}"/>')
    return f'<svg class="stars" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid slice" aria-hidden="true">{"".join(dots)}</svg>'


def slide(title: str, points: list[str], fig: str) -> str:
    lis = "".join(f"<li>{p}</li>" for p in points)
    return (f'<section class="slide"><h1>{title}</h1><div class="rule"></div>'
            f'<div class="body"><ul class="pts">{lis}</ul><div class="fig">{fig}</div></div></section>')


# ---------- 1. cover ----------

def cover() -> str:
    return (f'<section class="slide cover">{stars()}<div class="in">'
            f'<div class="tl"><svg class="mark" viewBox="5 4 14 18" aria-hidden="true"><path fill="currentColor" d="{TOWER}"/></svg>'
            f"<h1>Night's Watch</h1></div>"
            f'<p class="agenda">The problem · live demo · how it works · a year, simulated</p></div>'
            f'<div class="who">apar-t · rohitenterprise · vroy2008<br>'
            f'Long Horizon Agents Hackathon · San Francisco · 25 Sep 2026</div></section>')


# ---------- 2. problem ----------

def problem() -> str:
    rows = [("Amazon Web Services", "Ireland"), ("Google Cloud", "Belgium"), ("Twilio", "United States"),
            ("Orbit Data Services", "Brazil", "add"), ("Zendesk", "United States")]
    rr = "".join(f'<div class="r{" add" if len(r) > 2 else ""}"><span>{r[0]}</span><span>{r[1]}</span></div>' for r in rows)
    fig = (f'<div class="diff"><div class="hd"><span>larkspur.com/legal/subprocessors</span><b>changed 25 Sep</b></div>{rr}</div>'
           f'<p class="cav">One row added. No email sent. Fictional vendor.</p>')
    return slide("Vendors have vendors", [
        "Every vendor passes company data on to its own <b>sub-processors</b>.",
        "Vendors can change this list, or their <b>data policy</b>, <b>any day</b>. The only sign is an edit to a web page like this one.",
        "<b>GDPR Article 28</b> requires notice of the change, with time to object.",
        "The change turns up at the <b>next audit</b>, or from a customer.",
    ], fig)


# ---------- 3. live demo ----------

def demo() -> str:
    steps = [("1", "\u201cLast updated\u201d date changes", "dropped", ""),
             ("2", "DataHarvest Ltd added as a sub-processor", "R1 alert", "bad"),
             ("3", "Privacy policy now allows AI training", "R2 alert", "bad")]
    rr = "".join(f'<div class="r"><b>{n}</b><span>{w}</span><span class="out {c}">{o}</span></div>' for n, w, o, c in steps)
    fig = (f'<div class="diff steps"><div class="hd"><span>Tinybird (demo mirror)</span><b>one tick after each change</b></div>{rr}</div>'
           f'<p class="cav">The mirror is a copy on our own site. The real Tinybird page is not changed.</p>')
    return slide("Live demo", [
        "<b>Night\'s Watch Inc.</b> uses five vendors: AWS, Nimble, Tinybird, Liquid AI, Black Forest Labs.",
        "Its rules: <b>R1</b>&nbsp;approved countries only. <b>R2</b>&nbsp;no AI training on customer data. <b>R3</b>&nbsp;retention of 90 days or less.",
        "We make three changes to the <b>Tinybird demo mirror</b> and run a tick after each.",
        "A date edit is <b>dropped</b>. The other two turn the node <b>red</b>.",
    ], fig)


# ---------- 4. how it works ----------

def arrow(x1, y1, x2, y2, col=INK3, both=False) -> str:
    ang = math.atan2(y2 - y1, x2 - x1)

    def head(x, y, a):
        p1 = (x - 9 * math.cos(a - .45), y - 9 * math.sin(a - .45))
        p2 = (x - 9 * math.cos(a + .45), y - 9 * math.sin(a + .45))
        return f'<path d="M{p1[0]:.1f} {p1[1]:.1f}L{x} {y}L{p2[0]:.1f} {p2[1]:.1f}" fill="none" stroke="{col}" stroke-width="1.5"/>'
    s = f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-width="1.5"/>' + head(x2, y2, ang)
    if both:
        s += head(x1, y1, ang + math.pi)
    return s


def node(x, y, w, h, name, role, stroke=LINE2, dash="", dot=None, dim=False) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="rgba(14,22,40,.7)" stroke="{stroke}" stroke-width="1.3"{d}/>')
    tx = x + 20
    if dot:
        s += f'<circle cx="{x + 22}" cy="{y + 25}" r="5" fill="{dot}"/>'
        tx = x + 36
    s += f'<text x="{tx}" y="{y + 30}" font-size="18" font-weight="600" fill="{INK3 if dim else INK}">{name}</text>'
    s += f'<text class="m" x="{x + 20}" y="{y + 54}" font-size="13" fill="{INK3 if dim else INK2}">{role}</text>'
    return s


def pipeline() -> str:
    W, H, x, w, h, gap = 660, 470, 0, 330, 68, 32
    ys = [i * (h + gap) for i in range(5)]
    s = []
    s.append(node(x, ys[0], w, h, "Vendor pages", "sub-processor lists, privacy policies"))
    s.append(node(x, ys[1], w, h, "Nimble", "fetch and extract, every tick"))
    s.append(node(x, ys[2], w, h, "Liquid LFM2.5", "judges each new policy sentence"))
    s.append(node(x, ys[3], w, h, "Agent", "state card + change, rewrites card", stroke=ICE, dash="6 5"))
    s.append(node(x, ys[4], w, h, "Alert", "rule, before and after, proof", stroke="rgba(255,103,103,.6)", dot=RED))
    cx = x + w / 2
    for i in range(4):
        s.append(arrow(cx, ys[i] + h + 2, cx, ys[i + 1] - 3))
    s.append(f'<text class="m" x="{cx + 12}" y="{ys[2] - 11}" font-size="12.5" fill="{INK2}">changed</text>')
    s.append(f'<text class="m" x="{cx + 12}" y="{ys[4] - 11}" font-size="12.5" fill="{INK2}">breaks a rule</text>')
    sx, sw = 420, 240
    s.append(node(sx, ys[1], sw, h, "Dropped", "unchanged or date-only", dim=True))
    s.append(arrow(x + w + 3, ys[1] + h / 2, sx - 3, ys[1] + h / 2))
    s.append(f'<text class="m" x="{(x + w + sx) / 2}" y="{ys[1] + h / 2 - 9}" font-size="12.5" fill="{INK2}" text-anchor="middle">no change</text>')
    s.append(node(sx, ys[3], sw, h, "RawTree", "every snapshot, card, alert"))
    s.append(arrow(x + w + 3, ys[3] + h / 2, sx - 3, ys[3] + h / 2, both=True))
    s.append(f'<text class="m" x="{(x + w + sx) / 2}" y="{ys[3] + h / 2 - 9}" font-size="12.5" fill="{INK2}" text-anchor="middle">recall</text>')
    return f'<svg width="{W}" height="{H}" viewBox="-2 -2 {W + 4} {H + 4}" role="img" aria-label="Pipeline">{"".join(s)}</svg>'


def how() -> str:
    return slide("How it works", [
        "<b>Nimble</b> reads each vendor's <span class=nb>sub-processor</span> list and privacy policy.",
        "Unchanged pages and date-only edits are <b>dropped</b> before any model call.",
        "<b>Liquid LFM2.5</b> judges each new policy sentence. A second model reviews the few that would change a verdict.",
        "The agent starts from its <b>state card</b>, rewrites it, and pulls older history from <b>RawTree</b> only when it needs it.",
    ], pipeline())


# ---------- 5. a year, simulated ----------

def chart() -> str:
    d = json.loads((HERE / "series.json").read_text())
    rows, alerts = d["series"], d["alerts"]
    W, H = 690, 440
    x0, x1, y0, y1 = 58, 520, 34, 390
    XMAX, YMAX = 364, 450_000
    X = lambda day: x0 + (x1 - x0) * day / XMAX
    Y = lambda v: y1 - (y1 - y0) * v / YMAX
    s = []
    for v in (100_000, 200_000, 300_000, 400_000):
        s.append(f'<line x1="{x0}" x2="{x1}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{LINE}" stroke-width="1"/>')
        s.append(f'<text class="m" x="{x0 - 10}" y="{Y(v) + 4:.1f}" font-size="12" fill="{INK3}" text-anchor="end">{v // 1000}k</text>')
    s.append(f'<text class="m" x="{x0 - 10}" y="{y1 + 4}" font-size="12" fill="{INK3}" text-anchor="end">0</text>')
    s.append(f'<text class="m" x="{x0}" y="{y0 - 16}" font-size="12.5" fill="{INK2}">context tokens per day, all 6 vendors</text>')
    for t in (0, 90, 180, 270, 365):
        s.append(f'<text class="m" x="{X(min(t, XMAX)):.1f}" y="{y1 + 24}" font-size="12" fill="{INK3}" text-anchor="middle">{"day 0" if t == 0 else t}</text>')
    # Liquid's context window
    wy = Y(65_536)
    s.append(f'<line x1="{x0}" x2="{x1}" y1="{wy:.1f}" y2="{wy:.1f}" stroke="{INK2}" stroke-width="1" stroke-dasharray="5 5"/>')
    s.append(f'<text class="m" x="{x1 - 4}" y="{wy - 9:.1f}" font-size="12" fill="{INK2}" text-anchor="end">Liquid context window, 65,536</text>')
    # naive full history
    pn = "M" + "L".join(f"{X(r[0]):.1f} {Y(r[2]):.1f}" for r in rows)
    s.append(f'<path d="{pn}" fill="none" stroke="{NAIVE}" stroke-width="2" stroke-linejoin="round"/>')
    over = next(r for r in rows if r[2] > 65_536)
    s.append(f'<circle cx="{X(over[0]):.1f}" cy="{Y(over[2]):.1f}" r="5" fill="#050913" stroke="{INK}" stroke-width="1.6"/>')
    s.append(f'<text class="m" x="{X(over[0]) - 12:.1f}" y="{Y(over[2]) - 12:.1f}" font-size="12.5" fill="{INK}" text-anchor="end">day {over[0]}</text>')
    # Night's Watch
    pw = "M" + "L".join(f"{X(r[0]):.1f} {Y(r[1]):.1f}" for r in rows)
    s.append(f'<line x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}" stroke="{INK3}" stroke-width="1"/>')
    s.append(f'<path d="{pw}" fill="none" stroke="{ICE}" stroke-width="3" stroke-linejoin="round"/>')
    for a in alerts:
        s.append(f'<circle cx="{X(a[0]):.1f}" cy="{Y(1811) - 1:.1f}" r="5" fill="{RED}" stroke="#050913" stroke-width="2"/>')
    s.append(f'<text class="m" x="{X(alerts[2][0]):.1f}" y="{y1 - 16}" font-size="12" fill="{INK2}" text-anchor="middle">alerts</text>')
    # direct labels at the line ends
    last = rows[-1]
    s.append(f'<circle cx="{X(last[0]):.1f}" cy="{Y(last[2]):.1f}" r="4" fill="{NAIVE}"/>')
    s.append(f'<text x="{x1 + 14}" y="{Y(last[2]) - 2:.1f}" font-size="15" font-weight="600" fill="{INK}">Full history</text>')
    s.append(f'<text class="m" x="{x1 + 14}" y="{Y(last[2]) + 17:.1f}" font-size="12.5" fill="{INK2}">{last[2]:,} tokens</text>')
    s.append(f'<circle cx="{X(last[0]):.1f}" cy="{Y(last[1]):.1f}" r="4" fill="{ICE}"/>')
    s.append(f'<text x="{x1 + 14}" y="{Y(last[1]) - 24:.1f}" font-size="15" font-weight="600" fill="{INK}">Night\'s Watch</text>')
    s.append(f'<text class="m" x="{x1 + 14}" y="{Y(last[1]) - 5:.1f}" font-size="12.5" fill="{INK2}">{last[1]:,} tokens</text>')
    svg = f'<svg width="{W}" height="{H}" viewBox="-2 -2 {W + 4} {H + 4}" role="img" aria-label="Tokens per day over 365 days">{"".join(s)}</svg>'
    return svg + '<p class="cav">Simulated, 6 vendors, seed 7. Full-history tokens are computed with the same estimate, not run.</p>'


def year() -> str:
    return slide("A year, simulated", [
        "Six vendors, 365 days, five planted rule breaks. <b>5 of 5</b> caught, no false alerts.",
        "Context for all six state cards: <b>1,542 to 1,811 tokens</b>.",
        "An agent that keeps the full history needs <b>428,956 tokens</b> on day 365. It passes Liquid's window on <b>day 56</b>.",
        "8 crashes injected over 60 days. Memory came back <b>identical</b>.",
    ], chart())


def main() -> None:
    html = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Night\'s Watch</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=block">'
            f"<style>{CSS}</style></head><body>"
            + cover() + problem() + demo() + how() + year()
            + "</body></html>\n")
    OUT.write_text(html)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
