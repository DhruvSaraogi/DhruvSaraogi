#!/usr/bin/env python3
"""Render the animated SVG cards shown in the profile README.

    python render_cards.py            -> animated cards in cards/
    STATIC=1 python render_cards.py   -> final frame only, in cards-static/

Both runs also write preview.html, which lays the cards out the way the
README does, so you can open it in a browser and screenshot it.

Inputs (refreshed daily by .github/workflows/profile-cards.yml):
    data/contributions.json   {"total": n, "days": [{"date": "YYYY-MM-DD", "count": n}, ...]}
    data/repos.json           output of `gh repo list --json name,description,primaryLanguage,stargazerCount,pushedAt`

Cards are viewed through <img>, so they only use what works there: one inline
<style> block with CSS keyframes, SMIL <animate*> elements, and fonts subset
with pyftsubset and embedded as base64 WOFF data URIs. No scripts, no external
requests. Every animation turns off under prefers-reduced-motion.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import math
import os
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent
STATIC = os.environ.get("STATIC") == "1"
OUT = ROOT / ("cards-static" if STATIC else "cards")

# --------------------------------------------------------------------------
# Config: everything personal lives here.
# --------------------------------------------------------------------------
USER = "DhruvSaraogi"
NAME = "Dhruv Saraogi"
KICKER = "hello, i'm"
TAGLINE = "I build GPU simulations, web apps and the odd virtual machine."
PORTRAIT = ROOT / "assets" / "portrait.png"  # optional; .png or .jpg, square works best

COLORS = {
    "card": "#10151d",
    "border": "#222a36",
    "text": "#e9eef5",
    "muted": "#8b96a8",
    "faint": "#5b6577",
    "grid": "#1c232e",
    "empty": "#161c25",
    "brand": "#2dd4bf",
}

FONTS = {  # family name used in CSS -> source TTF (variable fonts are fine)
    "SG": ROOT / "fonts" / "src" / "SpaceGrotesk.ttf",
    "JB": ROOT / "fonts" / "src" / "JetBrainsMono.ttf",
}

# The three tiles under the hero. `href` is used by preview.html; keep the
# README links in step with it. `{repos}` expands to your public repo count.
TILES = [
    {"kicker": "01 / work", "title": "Projects", "sub": "{repos} public repositories",
     "href": f"https://github.com/{USER}?tab=repositories"},
    {"kicker": "02 / now", "title": "LC-3 VM", "sub": "Writing a virtual machine",
     "href": f"https://github.com/{USER}/lc3_virtual_machine"},
    {"kicker": "03 / contact", "title": "Say hello", "sub": "Find me on GitHub",
     "href": f"https://github.com/{USER}"},
]

# One card each. `blurb` overrides the repo description (yours are empty).
FEATURED = [
    {"repo": "GPU_SATELLITE_ORBIT_PREDICTOR", "title": "GPU Satellite Orbit Predictor",
     "blurb": "Simulates satellite motion under Earth gravity in CUDA and compares "
              "Euler with RK4 integration, with Python plots of the results."},
    {"repo": "Digital-Procrastination-Analyzer", "title": "Procrastination Analyzer",
     "blurb": "A Django app that tracks study sessions, distractions and habits, "
              "then points out procrastination patterns."},
    {"repo": "lc3_virtual_machine", "title": "LC-3 Virtual Machine",
     "blurb": "A virtual machine for the LC-3 teaching architecture. Work in progress."},
]

LANG_COLORS = {
    "Cuda": "#3A4E3A", "Python": "#3572A5", "Jupyter Notebook": "#DA5B0B",
    "JavaScript": "#f1e05a", "TypeScript": "#3178c6", "HTML": "#e34c26",
    "C": "#555555", "C++": "#f34b7d", "Rust": "#dea584", "Go": "#00ADD8",
}

W = 840          # every full-width card is 840 units wide
TILE_W = 272     # tiles sit three to a row at width="32.5%"

# Page timeline (seconds). All cards start loading together, so each card
# begins at its own offset and the page plays top to bottom.
T_HERO, T_TILES, T_ACTIVITY, T_REPOS, T_GRAPH = 0.0, 0.55, 1.1, 1.7, 2.6
T_TILE_STEP, T_REPO_STEP = 0.15, 0.3


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a toward b by t (0..1)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def anim(cls: str, delay: float) -> str:
    """class/style attributes for a CSS animation; nothing in STATIC mode."""
    if STATIC:
        return ""
    return f' class="{cls}" style="animation-delay:{delay:.2f}s"'


def smil(moving: str, still: str) -> str:
    """SMIL can't be stopped from CSS, so we ship both versions and let the
    reduced-motion media query pick. STATIC mode ships only the still one."""
    if STATIC:
        return still
    return f'<g class="smil">{moving}</g><g class="still">{still}</g>'


@lru_cache(maxsize=None)
def _metrics(family: str, weight: int):
    tt = TTFont(FONTS[family])
    if "fvar" in tt:
        axis = next(a for a in tt["fvar"].axes if a.axisTag == "wght")
        w = max(axis.minValue, min(axis.maxValue, weight))
        tt = instancer.instantiateVariableFont(tt, {"wght": w})
    return tt.getBestCmap(), tt["hmtx"].metrics, tt["head"].unitsPerEm


def text_width(s: str, family: str, size: float, weight: int = 400, spacing: float = 0) -> float:
    cmap, hmtx, upm = _metrics(family, weight)
    adv = sum(hmtx[cmap.get(ord(c), ".notdef")][0] for c in s)
    return adv * size / upm + spacing * len(s)


def wrap(s: str, family: str, size: float, max_w: float, weight: int = 400) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and text_width(trial, family, size, weight) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


class Card:
    """Collects markup plus the characters each font must keep."""

    def __init__(self, name: str, w: int, h: int, title: str):
        self.name, self.w, self.h, self.title = name, w, h, title
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.chars: dict[str, set[str]] = {f: set() for f in FONTS}

    def add(self, markup: str):
        self.parts.append(markup)

    def text(self, x, y, s, family="SG", size=14, weight=400, fill=None, anchor="start",
             spacing=0.0, extra="") -> str:
        s = str(s)
        self.chars[family].update(s)
        fill = fill or COLORS["text"]
        attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'class="{family.lower()}"',
                 f'font-size="{size}"', f'fill="{fill}"']
        if weight != 400:
            attrs.append(f'font-weight="{weight}"')
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if spacing:
            attrs.append(f'letter-spacing="{spacing}"')
        return f"<text {' '.join(attrs)}{extra}>{esc(s)}</text>"

    def frame(self, delay: float, w=None, h=None):
        w, h = w or self.w, h or self.h
        self.add(f'<g{anim("fade", delay)}><rect x="1" y="1" width="{w - 2}" height="{h - 2}" '
                 f'rx="14" fill="{COLORS["card"]}" stroke="{COLORS["border"]}"/></g>')

    def render(self) -> str:
        font_css = "".join(font_face(f, "".join(sorted(c))) for f, c in self.chars.items() if c)
        css = [font_css,
               ".sg{font-family:SG,ui-sans-serif,system-ui,sans-serif}",
               ".jb{font-family:JB,ui-monospace,monospace}"]
        if not STATIC:
            css.append(ANIM_CSS)
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{esc(self.title)}">'
                f"<title>{esc(self.title)}</title><style>{''.join(css)}</style>{defs}"
                f"{''.join(self.parts)}</svg>\n")


ANIM_CSS = (
    "@keyframes rise{from{opacity:0;transform:translateY(10px)}}"
    "@keyframes fade{from{opacity:0}}"
    "@keyframes grow{from{transform:scaleY(0)}}"
    "@keyframes pop{from{opacity:0;transform:scale(.2)}}"
    "@keyframes draw{from{stroke-dashoffset:1}}"
    ".rise{animation:rise .7s cubic-bezier(.2,.8,.2,1) both}"
    ".fade{animation:fade .8s ease both}"
    ".grow{transform-box:fill-box;transform-origin:50% 100%;animation:grow .9s cubic-bezier(.2,.8,.2,1) both}"
    ".pop{transform-box:fill-box;transform-origin:center;animation:pop .45s cubic-bezier(.3,1.5,.5,1) both}"
    ".draw{stroke-dasharray:1;animation:draw 1.6s ease-in-out both}"
    ".still{display:none}"
    "@media (prefers-reduced-motion:reduce){*{animation:none!important}.smil{display:none}.still{display:inline}}"
)


@lru_cache(maxsize=None)
def font_face(family: str, chars: str) -> str:
    """Subset with pyftsubset to exactly `chars`, embed as a WOFF data URI."""
    src = FONTS[family]
    tt = TTFont(src)
    cmap = tt.getBestCmap()
    missing = sorted({c for c in chars if ord(c) not in cmap and not c.isspace()})
    if missing:
        print(f"  ! {family} has no glyph for {''.join(missing)!r}", file=sys.stderr)
    wmin, wmax = 400, 400
    if "fvar" in tt:
        ax = next(a for a in tt["fvar"].axes if a.axisTag == "wght")
        wmin, wmax = int(ax.minValue), int(ax.maxValue)
    with tempfile.TemporaryDirectory() as tmp:
        txt, out = Path(tmp) / "chars.txt", Path(tmp) / "font.woff"
        txt.write_text(chars + " ", encoding="utf-8")
        subprocess.run(
            [sys.executable, "-m", "fontTools.subset", str(src),  # == pyftsubset
             f"--text-file={txt}", f"--output-file={out}", "--flavor=woff",
             "--layout-features=kern,liga,calt", "--no-hinting", "--desubroutinize",
             "--name-IDs=", "--notdef-outline"],
            check=True,
        )
        data = base64.b64encode(out.read_bytes()).decode()
    return (f"@font-face{{font-family:{family};font-weight:{wmin} {wmax};"
            f"src:url(data:font/woff;base64,{data}) format('woff')}}")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  ! {path.relative_to(ROOT)} missing, using empty data", file=sys.stderr)
        return default


def contribution_stats(days: list[dict]) -> dict:
    days = sorted(days, key=lambda d: d["date"])
    dates = [dt.date.fromisoformat(d["date"]) for d in days]
    counts = [int(d["count"]) for d in days]
    today = dates[-1] if dates else dt.date.today()

    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)

    # Today isn't over, so a streak that ran through yesterday still counts.
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:
        i -= 1
    current = 0
    while i >= 0 and counts[i]:
        current += 1
        i -= 1

    busiest = None
    if any(counts):
        j = max(range(len(counts)), key=lambda k: (counts[k], dates[k]))
        busiest = (dates[j], counts[j])

    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    per_month = {ym: 0 for ym in months}
    for d, c in zip(dates, counts):
        if (d.year, d.month) in per_month:
            per_month[(d.year, d.month)] += c

    return {
        "days": list(zip(dates, counts)),
        "active": sum(1 for c in counts if c),
        "current": current,
        "longest": longest,
        "busiest": busiest,
        "months": [(dt.date(y, m, 1), per_month[(y, m)]) for y, m in months],
        "year_total": sum(counts),
    }


def nice_step(top: int) -> int:
    for mag in (1, 10, 100, 1000, 10000):
        for s in (1, 2, 5):
            if top / (s * mag) <= 4:
                return s * mag
    return 10000


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
def ellipse_path(cx, cy, rx, ry) -> str:
    return (f"M{cx - rx},{cy} a{rx},{ry} 0 1,0 {2 * rx},0 a{rx},{ry} 0 1,0 {-2 * rx},0")


def hero_card() -> Card:
    H = 232
    c = Card("hero", W, H, f"{NAME}. {TAGLINE}")
    t0 = T_HERO
    c.frame(t0)
    brand, faint = COLORS["brand"], COLORS["faint"]

    # Orbit art on the right
    cx, cy = 690, 116
    c.defs.append(f'<radialGradient id="planet" cx="35%" cy="30%" r="75%">'
                  f'<stop offset="0" stop-color="{mix(brand, "#ffffff", .35)}"/>'
                  f'<stop offset="1" stop-color="{mix(brand, COLORS["card"], .55)}"/></radialGradient>')
    stars = [(560, 52, 1.2, 0.2), (612, 186, 1.0, 1.1), (792, 60, 1.4, 0.6),
             (780, 178, 1.0, 1.6), (650, 40, 0.9, 0.9), (812, 120, 1.1, 2.0)]
    for x, y, r, ph in stars:
        dot = f'<circle cx="{x}" cy="{y}" r="{r}" fill="{COLORS["muted"]}"'
        c.add(f'<g{anim("fade", t0 + .6 + ph * .2)}>' + smil(
            f'{dot}><animate attributeName="opacity" values=".2;1;.2" dur="{2.6 + ph}s" '
            f'begin="{ph}s" repeatCount="indefinite"/></circle>',
            f'{dot} opacity=".6"/>') + "</g>")

    orbits = [  # rx, ry, tilt, satellite period, reverse, start point (0..1) for the still frame
        (128, 40, -16, 9, False, .62),
        (82, 26, 24, 6, True, .18),
    ]
    for k, (rx, ry, tilt, period, rev, at) in enumerate(orbits):
        d = ellipse_path(0, 0, rx, ry)
        c.add(f'<g transform="translate({cx} {cy}) rotate({tilt})">'
              f'<path d="{d}" pathLength="1" fill="none" stroke="{faint}" stroke-width="1"'
              f'{anim("draw", t0 + .3 + k * .25)}/>')
        a = 2 * math.pi * at  # where the satellite sits in the still frame
        sx, sy = rx * math.cos(a), ry * math.sin(a)
        sat = f'<circle r="{4.5 if k == 0 else 3.5}" fill="{brand}"'
        keys = ' keyPoints="1;0" keyTimes="0;1" calcMode="linear"' if rev else ""
        moving = (f'{sat}><animateMotion dur="{period}s" begin="{t0 + 1.4:.2f}s" '
                  f'repeatCount="indefinite" path="{d}"{keys}/></circle>')
        still = f'{sat} cx="{sx:.1f}" cy="{sy:.1f}"/>'
        if STATIC:
            c.add(still)
        else:  # the moving satellite fades in once its orbit has drawn
            c.add(f'<g{anim("fade", t0 + 1.4)}>{smil(moving, still)}</g>')
        c.add("</g>")
    c.add(f'<g{anim("pop", t0 + .5)}><circle cx="{cx}" cy="{cy}" r="26" fill="url(#planet)"/></g>')

    # Optional portrait
    x = 44
    if PORTRAIT.exists():
        mime = "image/jpeg" if PORTRAIT.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        b64 = base64.b64encode(PORTRAIT.read_bytes()).decode()
        c.defs.append('<clipPath id="pc"><circle cx="104" cy="116" r="60"/></clipPath>')
        c.add(f'<g{anim("pop", t0 + .2)}><image href="data:{mime};base64,{b64}" x="44" y="56" '
              f'width="120" height="120" clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"/>'
              f'<circle cx="104" cy="116" r="60" fill="none" stroke="{brand}" stroke-width="2"/></g>')
        x = 190

    c.add(f'<g{anim("rise", t0 + .15)}>' +
          c.text(x, 78, KICKER.upper(), "JB", 13, 500, brand, spacing=2.5) + "</g>")
    c.add(f'<g{anim("rise", t0 + .3)}>' + c.text(x - 2, 130, NAME, "SG", 52, 700) + "</g>")
    cursor_x = x + text_width(NAME, "SG", 52, 700) + 6
    bar = f'<rect x="{cursor_x:.1f}" y="92" width="4" height="44" rx="1" fill="{brand}"'
    c.add(f'<g{anim("fade", t0 + .5)}>' + smil(
        f'{bar}><animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.5;.5;1" '
        f'dur="1.1s" begin="{t0 + .9:.2f}s" repeatCount="indefinite"/></rect>',
        f"{bar}/>") + "</g>")
    # Keep the tagline clear of the orbit art: shrink to 15px, then wrap.
    room = 536 - x
    size = 18
    while size > 15 and text_width(TAGLINE, "SG", size) > room:
        size -= 0.5
    lines = wrap(TAGLINE, "SG", size, room)[:2]
    for n, line in enumerate(lines):
        y = 168 - (len(lines) - 1) * 10 + n * (size + 5)
        c.add(f'<g{anim("rise", t0 + .45 + n * .08)}>' +
              c.text(x, y, line, "SG", size, 400, COLORS["muted"]) + "</g>")
    c.add(f'<g{anim("rise", t0 + .6)}>' +
          c.text(x, 198, f"github.com/{USER}", "JB", 12, 400, faint) + "</g>")
    return c


ARROW = "M0 0h14M8-6l6 6-6 6"


def tile_card(i: int, tile: dict, repo_count: int) -> Card:
    H = 116
    t0 = T_TILES + i * T_TILE_STEP
    sub = tile["sub"].format(repos=repo_count)
    c = Card(f"tile-{i + 1}", TILE_W, H, f'{tile["title"]}: {sub}')
    c.frame(t0)
    brand = COLORS["brand"]
    c.add(f'<g{anim("rise", t0 + .1)}>' +
          c.text(22, 36, tile["kicker"].upper(), "JB", 11, 500, brand, spacing=1.5) + "</g>")
    c.add(f'<g{anim("rise", t0 + .2)}>' + c.text(22, 70, tile["title"], "SG", 24, 700) + "</g>")
    c.add(f'<g{anim("rise", t0 + .3)}>' + c.text(22, 94, sub, "SG", 13, 400, COLORS["muted"]) + "</g>")
    arrow = (f'<path d="{ARROW}" fill="none" stroke="{brand}" stroke-width="2" '
             f'stroke-linecap="round" stroke-linejoin="round"')
    c.add(f'<g transform="translate({TILE_W - 42} 36)"><g{anim("fade", t0 + .4)}>' + smil(
        f'{arrow}><animateTransform attributeName="transform" type="translate" '
        f'values="0 0;4 0;0 0" dur="1.8s" begin="{t0 + 1:.2f}s" repeatCount="indefinite"/></path>',
        f"{arrow}/>") + "</g></g>")
    return c


def activity_card(st: dict) -> Card:
    H = 300
    t0 = T_ACTIVITY
    months = st["months"]
    total12 = sum(v for _, v in months)
    c = Card("activity", W, H, f"{total12} contributions in the last 12 months")
    c.frame(t0)
    brand, muted, faint = COLORS["brand"], COLORS["muted"], COLORS["faint"]

    c.add(f'<g{anim("rise", t0 + .1)}>' +
          c.text(40, 46, "ACTIVITY · LAST 12 MONTHS", "JB", 11, 500, brand, spacing=1.5) + "</g>")
    label = f"{total12} contribution{'s' if total12 != 1 else ''}"
    c.add(f'<g{anim("rise", t0 + .2)}>' + c.text(40, 78, label, "SG", 24, 700) + "</g>")

    # Bars
    x0, x1, top, base = 52, 520, 112, 244
    peak = max([v for _, v in months] + [1])
    step = nice_step(peak)
    ymax = step * -(-peak // step)
    for g in range(0, ymax + 1, step):
        y = base - (base - top) * g / ymax
        c.add(f'<g{anim("fade", t0 + .25)}><line x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}" '
              f'stroke="{COLORS["grid"]}"/>' +
              c.text(x0 - 10, y + 4, g, "JB", 10, 400, faint, "end") + "</g>")
    slot = (x1 - x0) / len(months)
    bw = 20
    for k, (d, v) in enumerate(months):
        bx = x0 + slot * k + (slot - bw) / 2
        h = (base - top) * v / ymax
        delay = t0 + .35 + k * .05
        if v:
            c.add(f'<g{anim("grow", delay)}><rect x="{bx:.1f}" y="{base - h:.1f}" width="{bw}" '
                  f'height="{h:.1f}" rx="3" fill="{brand}"/></g>')
            c.add(f'<g{anim("rise", delay + .5)}>' +
                  c.text(bx + bw / 2, base - h - 7, v, "JB", 10, 500, COLORS["text"], "middle") + "</g>")
        else:
            c.add(f'<g{anim("fade", delay)}><rect x="{bx:.1f}" y="{base - 2}" width="{bw}" height="2" '
                  f'rx="1" fill="{COLORS["border"]}"/></g>')
        mlabel = d.strftime("%b")
        c.add(f'<g{anim("fade", delay)}>' +
              c.text(bx + bw / 2, base + 20, mlabel, "JB", 10, 400,
                     muted if k == len(months) - 1 else faint, "middle") + "</g>")

    # Divider + stats
    c.add(f'<g{anim("fade", t0 + .3)}><line x1="556" x2="556" y1="40" y2="{H - 40}" '
          f'stroke="{COLORS["border"]}"/></g>')
    busiest = st["busiest"]
    stats = [
        ("current streak", st["current"], "day" if st["current"] == 1 else "days"),
        ("longest streak", st["longest"], "day" if st["longest"] == 1 else "days"),
        ("active days", st["active"], f"of {len(st['days'])}"),
        ("busiest day", f"{busiest[0]:%b} {busiest[0].day}" if busiest else "—",
         (f"{busiest[1]} contribution{'s' if busiest[1] != 1 else ''}" if busiest else "")),
    ]
    for k, (lab, val, unit) in enumerate(stats):
        sx, sy = 584 + (k % 2) * 124, 104 + (k // 2) * 100
        delay = t0 + .6 + k * .12
        val = str(val)
        big = c.text(sx, sy, val, "SG", 30, 700)
        if k == 3:  # busiest day: unit goes underneath
            unit_el = c.text(sx, sy + 20, unit, "JB", 10, 400, faint)
            lab_y = sy + 38
        else:
            unit_el = c.text(sx + text_width(val, "SG", 30, 700) + 6, sy, unit, "JB", 11, 400, muted)
            lab_y = sy + 22
        c.add(f'<g{anim("rise", delay)}>{big}{unit_el}' +
              c.text(sx, lab_y, lab.upper(), "JB", 9.5, 500, faint, spacing=1.2) + "</g>")
        if k == 0 and st["current"]:
            px, py = sx - 14, sy - 10
            ring = f'<circle cx="{px}" cy="{py}" r="4" fill="none" stroke="{brand}"'
            c.add(f'<g{anim("fade", delay)}><circle cx="{px}" cy="{py}" r="3" fill="{brand}"/>' + smil(
                f'{ring}><animate attributeName="r" values="3;9" dur="1.6s" begin="{delay + .5:.2f}s" '
                f'repeatCount="indefinite"/><animate attributeName="opacity" values="1;0" dur="1.6s" '
                f'begin="{delay + .5:.2f}s" repeatCount="indefinite"/></circle>', "") + "</g>")
    return c


STAR = ("M6 0l1.8 3.9 4.2.5-3.1 2.9.8 4.2L6 9.4 2.3 11.5l.8-4.2L0 4.4l4.2-.5z")


def repo_card(i: int, feat: dict, repos: dict) -> Card:
    H = 152
    t0 = T_REPOS + i * T_REPO_STEP
    meta = repos.get(feat["repo"], {})
    title = feat.get("title") or feat["repo"]
    blurb = feat.get("blurb") or meta.get("description") or ""
    c = Card(f"repo-{i + 1}", W, H, f"{title}. {blurb}")
    c.frame(t0)
    brand, muted, faint = COLORS["brand"], COLORS["muted"], COLORS["faint"]

    c.add(f'<g{anim("grow", t0 + .15)}><rect x="26" y="30" width="3" height="{H - 60}" rx="1.5" '
          f'fill="{brand}"/></g>')
    c.add(f'<g{anim("rise", t0 + .2)}>' +
          c.text(48, 44, f"FEATURED · {i + 1:02d}", "JB", 11, 500, brand, spacing=1.5) + "</g>")
    c.add(f'<g{anim("rise", t0 + .3)}>' + c.text(48, 78, title, "SG", 24, 700) + "</g>")
    for n, line in enumerate(wrap(blurb, "SG", 14, 560)[:2]):
        c.add(f'<g{anim("rise", t0 + .4 + n * .08)}>' +
              c.text(48, 106 + n * 20, line, "SG", 14, 400, muted) + "</g>")

    # Right column: language, stars, last push (right-aligned)
    rx = W - 40
    rows = []
    lang = (meta.get("primaryLanguage") or {}).get("name")
    if lang:
        rows.append(("lang", lang))
    rows.append(("star", str(meta.get("stargazerCount", 0))))
    if meta.get("pushedAt"):
        p = dt.date.fromisoformat(meta["pushedAt"][:10])
        rows.append(("push", f"updated {p:%b %Y}"))
    for n, (kind, val) in enumerate(rows):
        y = 46 + n * 28
        w = text_width(val, "JB", 12)
        el = c.text(rx, y, val, "JB", 12, 400, muted if kind != "push" else faint, "end")
        ix = rx - w - 18
        if kind == "lang":
            icon = f'<circle cx="{ix + 5:.1f}" cy="{y - 4}" r="5" fill="{LANG_COLORS.get(val, faint)}"/>'
        elif kind == "star":
            icon = (f'<path d="{STAR}" transform="translate({ix:.1f} {y - 10})" fill="none" '
                    f'stroke="{muted}" stroke-width="1.2" stroke-linejoin="round"/>')
        else:
            icon = ""
        c.add(f'<g{anim("rise", t0 + .45 + n * .08)}>{icon}{el}</g>')
    return c


def graph_card(st: dict) -> Card:
    days = st["days"]
    cell, gap = 10, 3
    pitch = cell + gap
    offset = (days[0][0].weekday() + 1) % 7 if days else 0  # GitHub weeks start on Sunday
    cols = (len(days) + offset + 6) // 7 or 1
    grid_w = cols * pitch - gap
    x0 = (W - grid_w) / 2 + 14
    y0 = 84
    H = y0 + 7 * pitch - gap + 56
    total = st["year_total"]
    c = Card("graph", W, H, f"Contribution graph: {total} contributions in the last year")
    t0 = T_GRAPH
    c.frame(t0)
    brand, faint, muted = COLORS["brand"], COLORS["faint"], COLORS["muted"]
    levels = [COLORS["empty"]] + [mix(COLORS["card"], brand, t) for t in (.3, .55, .8, 1)]

    c.add(f'<g{anim("rise", t0 + .1)}>' +
          c.text(40, 46, "CONTRIBUTION GRAPH", "JB", 11, 500, brand, spacing=1.5) + "</g>")

    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(f'<g{anim("fade", t0 + .2)}>' +
              c.text(x0 - 10, y0 + row * pitch + cell - 1, lab, "JB", 9, 400, faint, "end") + "</g>")

    peak = max([n for _, n in days] + [1])
    last_label_col = -9
    cells = []
    for idx, (d, n) in enumerate(days):
        pos = idx + offset
        col, row = divmod(pos, 7)
        if d.day == 1 or idx == 0:
            lc = col if row == 0 or idx == 0 else col + 1
            if lc - last_label_col >= 3 and lc < cols:
                c.add(f'<g{anim("fade", t0 + .2 + lc * .02)}>' +
                      c.text(x0 + lc * pitch, y0 - 10, f"{d:%b}", "JB", 9, 400, faint) + "</g>")
                last_label_col = lc
        lvl = 0 if n == 0 else max(1, math.ceil(4 * n / peak))
        a = anim("pop", t0 + .3 + col * .022 + row * .012)
        cells.append(f'<rect x="{x0 + col * pitch:.1f}" y="{y0 + row * pitch}" width="{cell}" '
                     f'height="{cell}" rx="2" fill="{levels[lvl]}"{a}>'
                     f"<title>{n} on {d:%Y-%m-%d}</title></rect>" if n else
                     f'<rect x="{x0 + col * pitch:.1f}" y="{y0 + row * pitch}" width="{cell}" '
                     f'height="{cell}" rx="2" fill="{levels[0]}"{a}/>')
    c.add("".join(cells))

    fy = H - 26
    c.add(f'<g{anim("fade", t0 + 1.6)}>' +
          c.text(x0, fy, f"{total} contribution{'s' if total != 1 else ''} in the last year",
                 "JB", 11, 400, muted) + "</g>")
    lx = x0 + grid_w - (5 * pitch - gap)
    legend = c.text(lx - 8, fy, "Less", "JB", 10, 400, faint, "end")
    legend += "".join(f'<rect x="{lx + k * pitch}" y="{fy - 9}" width="{cell}" height="{cell}" rx="2" '
                      f'fill="{col}"/>' for k, col in enumerate(levels))
    legend += c.text(lx + 5 * pitch + 5, fy, "More", "JB", 10, 400, faint)
    c.add(f'<g{anim("fade", t0 + 1.7)}>{legend}</g>')
    return c


# --------------------------------------------------------------------------
# Preview page (mirrors the README layout)
# --------------------------------------------------------------------------
def preview(cards: list[Card]):
    rel = OUT.name
    by = {c.name: c for c in cards}
    img = lambda n, w="100%": f'<img src="{rel}/{n}.svg" width="{w}" alt="{esc(by[n].title)}">'
    tiles = "\n".join(f'<a href="{t["href"]}">{img(f"tile-{i + 1}", "32.5%")}</a>'
                      for i, t in enumerate(TILES))
    repos = "\n".join(f'<a href="https://github.com/{USER}/{f["repo"]}">{img(f"repo-{i + 1}")}</a>'
                      for i, f in enumerate(FEATURED))
    page = f"""<!doctype html><meta charset="utf-8"><title>Profile cards preview</title>
<style>body{{margin:0;background:#0d1117;display:flex;justify-content:center}}
main{{width:min(888px,100% - 32px);padding:32px 0}}p{{margin:0 0 12px;text-align:center}}
img{{display:inline-block;vertical-align:top}}</style>
<main><p>{img("hero")}</p><p>{tiles}</p><p>{img("activity")}</p><p>{repos}</p><p>{img("graph")}</p></main>
"""
    (ROOT / "preview.html").write_text(page, encoding="utf-8", newline="\n")


def main():
    contrib = load_json(ROOT / "data" / "contributions.json", {"days": []})
    repos = {r["name"]: r for r in load_json(ROOT / "data" / "repos.json", [])}
    st = contribution_stats(contrib.get("days", []))

    cards = [hero_card()]
    cards += [tile_card(i, t, len(repos)) for i, t in enumerate(TILES)]
    cards.append(activity_card(st))
    cards += [repo_card(i, f, repos) for i, f in enumerate(FEATURED)]
    cards.append(graph_card(st))

    OUT.mkdir(exist_ok=True)
    keep = {f"{c.name}.svg" for c in cards}
    for old in OUT.glob("*.svg"):
        if old.name not in keep:
            old.unlink()
    for c in cards:
        svg = c.render()
        (OUT / f"{c.name}.svg").write_text(svg, encoding="utf-8", newline="\n")
        print(f"  {OUT.name}/{c.name}.svg  {len(svg.encode()) / 1024:.1f} KB")
    preview(cards)
    print(f"{'static' if STATIC else 'animated'} cards written; open preview.html to look")


if __name__ == "__main__":
    main()
