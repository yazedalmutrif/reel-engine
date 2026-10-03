"""Inline-SVG charts for the dashboard, built in Python (no JS, CSP-safe).

Design rules (dataviz skill): the form follows the data's job; one hue (the brand
violet, stepped to #AD66FA so marks sit inside the dark-mode lightness band) plus a
gray baseline ("emphasis" form) instead of a categorical palette; status colours are
reserved for state and always ship with an icon + label in the HTML; thin marks
(2px lines, <=24px bars with 4px rounded data-ends, >=8px dots with a 2px surface
ring); solid hairline grid; text wears text colours, never the series colour; every
chart has a <title>/<desc>, per-mark hover titles, and a table view in the template.

Everything here returns markupsafe.Markup. No inline style attributes: colours and
sizes are SVG presentation attributes, fonts come from the stylesheet (svg text).
"""
from __future__ import annotations

import math
import re
from typing import Callable, Iterable, Sequence

from markupsafe import Markup, escape

# ---- tokens (kept in sync with static/app.css) ------------------------------------
ACCENT = "#AD66FA"      # chart-mark step of the brand violet #B570FF
BASELINE = "#8B8B98"    # de-emphasis gray (account median, context)
SURFACE = "#1B1B1F"     # card surface: used for the 2px ring / gap around marks
GRID = "#2A2A31"        # hairline gridlines
AXIS = "#3A3A44"        # baseline / axis rule
INK = "#F2F2F5"         # primary text
INK2 = "#C4C4CE"        # secondary text
MUTED = "#9A9AA6"       # axis labels
EMPTY_CELL = "#26262C"  # a slot with no data
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
NEUTRAL_STATE = "#62626F"  # skipped / locked / paused ticks

Fmt = Callable[[float], str]


def _e(s: object) -> str:
    return str(escape("" if s is None else s))


def _n(v: float) -> str:
    """Compact SVG coordinate."""
    return f"{v:.1f}".rstrip("0").rstrip(".") if not float(v).is_integer() else str(int(v))


# ---- number formatting --------------------------------------------------------------
def num_or_none(v: object) -> float | None:
    """Template values may be None or jinja Undefined; only real numbers get formatted."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def fmt_int(v: float | None) -> str:
    v = num_or_none(v)
    return "–" if v is None else f"{round(v):,}"


def fmt_compact(v: float | None) -> str:
    v = num_or_none(v)
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 10_000:
        return f"{v / 1000:.0f}K"
    if a >= 1000:
        return f"{v / 1000:.1f}K".replace(".0K", "K")
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.1f}"


def fmt_pct(v: float | None, digits: int = 0) -> str:
    v = num_or_none(v)
    return "–" if v is None else f"{v * 100:.{digits}f}%"


def nice_ticks(lo: float, hi: float, count: int = 4, min_step: float = 0) -> list[float]:
    """Round tick values covering [lo, hi] (0 / 250 / 500 ...). min_step=1 keeps counts whole."""
    if hi <= lo:
        hi = lo + (abs(lo) or 1)
    span = hi - lo
    raw = span / max(count, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= count:
            break
    step = max(step, min_step)
    start = math.floor(lo / step + 1e-9) * step
    ticks = []
    v = start
    while v < hi - 1e-9:
        ticks.append(round(v, 10))
        v += step
    ticks.append(round(v, 10))
    return ticks


def _safe_id(uid: str) -> str:
    """ids can carry DB values (e.g. a bandit dimension); keep them to a safe charset."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(uid)) or "c"


def _svg_open(uid: str, width: float, height: float, title: str, desc: str, cls: str = "chart") -> str:
    uid = _safe_id(uid)
    return (
        f'<svg class="{cls}" viewBox="0 0 {_n(width)} {_n(height)}" width="{_n(width)}" height="{_n(height)}" '
        f'role="img" aria-labelledby="{uid}-t {uid}-d" focusable="false">'
        f'<title id="{uid}-t">{_e(title)}</title><desc id="{uid}-d">{_e(desc)}</desc>'
    )


def _text_w(s: str, px: float = 7.2) -> float:
    """Rough width of mono text at 12px."""
    return len(s) * px


def _rounded_col(x: float, y: float, w: float, base: float, r: float = 4) -> str:
    """Column path: 4px rounded data-end, square at the baseline."""
    h = base - y
    r = max(0.0, min(r, w / 2, h))
    return (
        f"M{_n(x)},{_n(base)} V{_n(y + r)} Q{_n(x)},{_n(y)} {_n(x + r)},{_n(y)} "
        f"H{_n(x + w - r)} Q{_n(x + w)},{_n(y)} {_n(x + w)},{_n(y + r)} V{_n(base)} Z"
    )


# ---- line chart ---------------------------------------------------------------------
def line_chart(
    uid: str,
    title: str,
    desc: str,
    x_labels: Sequence[str],
    series: Sequence[dict],
    *,
    width: float = 560,
    height: float = 220,
    y_fmt: Fmt = fmt_compact,
    zero_based: bool = True,
    max_x_labels: int | None = None,
    markers: Iterable[dict] = (),
    tip_labels: Sequence[str] | None = None,
) -> Markup:
    """Line chart over evenly spaced x positions.

    series: [{name, values:[float|None], color, emphasis:bool, area:bool, end_label:bool}]
    markers: [{i, value, color, tip}] drawn as ringed dots (e.g. errors).
    Returns '' when there is nothing to plot (the template shows the empty state).
    """
    n = len(x_labels)
    vals = [v for s in series for v in s["values"] if v is not None]
    if n == 0 or not vals:
        return Markup("")
    lo = 0.0 if zero_based else min(vals)
    hi = max(vals)
    if not zero_based and hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    whole = all(float(v).is_integer() for v in vals)
    ticks = nice_ticks(lo, hi, 4, min_step=1 if whole else 0)
    y0, y1 = ticks[0], ticks[-1]
    tick_txt = [y_fmt(t) for t in ticks]
    pad_l = max(_text_w(t) for t in tick_txt) + 12
    ends = [s for s in series if s.get("end_label", s.get("emphasis"))]
    pad_r = 14 + (max((_text_w(y_fmt(_last(s["values"]) or 0)) for s in ends), default=0) + 8 if ends else 0)
    pad_t, pad_b = 12, 28
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

    def X(i: int) -> float:
        return pad_l + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v: float) -> float:
        return pad_t + ph - (v - y0) / (y1 - y0) * ph

    out = [_svg_open(uid, width, height, title, desc)]
    # grid + y labels
    for t, s in zip(ticks, tick_txt):
        y = Y(t)
        out.append(f'<line x1="{_n(pad_l)}" x2="{_n(pad_l + pw)}" y1="{_n(y)}" y2="{_n(y)}" stroke="{AXIS if t == y0 else GRID}" stroke-width="1"/>')
        out.append(f'<text class="ax" x="{_n(pad_l - 8)}" y="{_n(y + 4)}" text-anchor="end" fill="{MUTED}">{_e(s)}</text>')
    # x labels
    lab_w = max(_text_w(str(x)) for x in x_labels) + 12  # labels never closer than their own width
    cap = min(max_x_labels or 99, max(2, int(pw // lab_w)))
    step = max(1, math.ceil(n / cap))
    idxs = list(range(0, n, step))
    if n - 1 not in idxs:
        # keep the last label; drop the previous one if the two would collide
        min_gap = max(_text_w(x_labels[-1]), _text_w(x_labels[idxs[-1]])) + 14
        if idxs and X(n - 1) - X(idxs[-1]) < min_gap:
            idxs[-1] = n - 1
        else:
            idxs.append(n - 1)
    # drop any label whose box would touch its neighbour's (the last label always wins)
    kept: list[tuple[int, str, float]] = []
    for i in idxs:
        anchor = "start" if (i == 0 and n > 1) else ("end" if (i == n - 1 and n > 1) else "middle")
        w = _text_w(str(x_labels[i]))
        x0 = X(i) if anchor == "start" else (X(i) - w if anchor == "end" else X(i) - w / 2)
        while kept and x0 < kept[-1][2] + 10:
            if i != n - 1:
                break
            kept.pop()
        else:
            kept.append((i, anchor, x0 + w))
    for i, anchor, _ in kept:
        out.append(f'<text class="ax" x="{_n(X(i))}" y="{_n(height - 8)}" text-anchor="{anchor}" fill="{MUTED}">{_e(x_labels[i])}</text>')
    # series: de-emphasised first so the emphasis line sits on top
    ordered = sorted(series, key=lambda s: bool(s.get("emphasis")))
    for s in ordered:
        color = s.get("color", ACCENT)
        segs = _segments(s["values"])
        for seg in segs:
            if s.get("area") and len(seg) > 1:
                d = f"M{_n(X(seg[0][0]))},{_n(Y(y0))} " + " ".join(f"L{_n(X(i))},{_n(Y(v))}" for i, v in seg) + f" L{_n(X(seg[-1][0]))},{_n(Y(y0))} Z"
                out.append(f'<path d="{d}" fill="{color}" fill-opacity="0.10" stroke="none"/>')
            if len(seg) > 1:
                d = "M" + " L".join(f"{_n(X(i))},{_n(Y(v))}" for i, v in seg)
                out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
            else:
                i, v = seg[0]
                out.append(f'<circle cx="{_n(X(i))}" cy="{_n(Y(v))}" r="3" fill="{color}"/>')
        last = _last_idx(s["values"])
        if last is not None and s.get("end_label", s.get("emphasis")):
            lx, ly = X(last), Y(s["values"][last])
            out.append(f'<circle cx="{_n(lx)}" cy="{_n(ly)}" r="4" fill="{color}" stroke="{SURFACE}" stroke-width="2"/>')
            out.append(f'<text class="val" x="{_n(lx + 8)}" y="{_n(ly + 4)}" fill="{INK}">{_e(y_fmt(s["values"][last]))}</text>')
    for m in markers:
        mx, my = X(m["i"]), Y(m["value"])
        out.append(f'<circle cx="{_n(mx)}" cy="{_n(my)}" r="4.5" fill="{m.get("color", STATUS["critical"])}" stroke="{SURFACE}" stroke-width="2"><title>{_e(m.get("tip", ""))}</title></circle>')
    # hover layer: one hit column per x listing every series (tooltips enhance, never gate)
    col = pw / (n - 1) if n > 1 else pw
    tips = tip_labels or x_labels
    for i in range(n):
        rows = [f"{s['name']}: {y_fmt(s['values'][i]) if s['values'][i] is not None else 'no data'}" for s in series]
        x = X(i) - col / 2
        out.append(
            f'<rect class="hit" x="{_n(max(pad_l, x))}" y="{_n(pad_t)}" width="{_n(min(col, pad_l + pw - max(pad_l, x)) if n > 1 else pw)}" '
            f'height="{_n(ph)}" fill="transparent"><title>{_e(tips[i])}&#10;{_e(chr(10).join(rows))}</title></rect>'
        )
    out.append("</svg>")
    return Markup("".join(out))  # nosec B704 - text escaped via _e()


def _segments(values: Sequence[float | None]) -> list[list[tuple[int, float]]]:
    segs: list[list[tuple[int, float]]] = []
    cur: list[tuple[int, float]] = []
    for i, v in enumerate(values):
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append((i, float(v)))
    if cur:
        segs.append(cur)
    return segs


def _last_idx(values: Sequence[float | None]) -> int | None:
    for i in range(len(values) - 1, -1, -1):
        if values[i] is not None:
            return i
    return None


def _last(values: Sequence[float | None]) -> float | None:
    i = _last_idx(values)
    return None if i is None else values[i]


# ---- column chart -------------------------------------------------------------------
def column_chart(
    uid: str,
    title: str,
    desc: str,
    items: Sequence[dict],
    *,
    width: float = 560,
    height: float = 220,
    y_fmt: Fmt = fmt_compact,
    label_all: bool = False,
    max_x_labels: int | None = None,
) -> Markup:
    """Single-series columns. items: [{label, value, tip}]. Labels only the max and the latest."""
    vals = [it["value"] for it in items if it.get("value") is not None]
    if not items or not vals:
        return Markup("")
    whole = all(float(v).is_integer() for v in vals)
    ticks = nice_ticks(0, max(max(vals), 1), 4, min_step=1 if whole else 0)
    y1 = ticks[-1]
    tick_txt = [y_fmt(t) for t in ticks]
    pad_l = max(_text_w(t) for t in tick_txt) + 12
    pad_r, pad_t, pad_b = 8, 20, 28
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(items)
    slot = pw / n
    bw = min(24.0, max(2.0, slot - 2 if slot < 6 else slot * 0.62))
    base = pad_t + ph

    def Y(v: float) -> float:
        return pad_t + ph - v / y1 * ph

    out = [_svg_open(uid, width, height, title, desc)]
    for t, s in zip(ticks, tick_txt):
        y = Y(t)
        out.append(f'<line x1="{_n(pad_l)}" x2="{_n(pad_l + pw)}" y1="{_n(y)}" y2="{_n(y)}" stroke="{AXIS if t == 0 else GRID}" stroke-width="1"/>')
        out.append(f'<text class="ax" x="{_n(pad_l - 8)}" y="{_n(y + 4)}" text-anchor="end" fill="{MUTED}">{_e(s)}</text>')
    imax = max(range(n), key=lambda i: items[i].get("value") or 0)
    labelled = set(range(n)) if label_all else {imax, n - 1}
    cap = min(max_x_labels or 99, max(2, int(pw // (max(_text_w(str(it["label"])) for it in items) + 14))))
    step = max(1, math.ceil(n / cap))
    for i, it in enumerate(items):
        v = it.get("value")
        cx = pad_l + slot * i + slot / 2
        x = cx - bw / 2
        if v is not None and v > 0:
            out.append(f'<path class="mark" d="{_rounded_col(x, Y(v), bw, base)}" fill="{ACCENT}"/>')
        if v is not None and i in labelled:
            out.append(f'<text class="val" x="{_n(cx)}" y="{_n(Y(v) - 6)}" text-anchor="middle" fill="{INK}">{_e(y_fmt(v))}</text>')
        if i % step == 0 or i == n - 1:
            if i == n - 1 or (n - 1 - i) >= step * 0.6:
                out.append(f'<text class="ax" x="{_n(cx)}" y="{_n(height - 8)}" text-anchor="middle" fill="{MUTED}">{_e(it["label"])}</text>')
        tip = it.get("tip") or f"{it['label']}: {y_fmt(v) if v is not None else 'no data'}"
        out.append(f'<rect class="hit" x="{_n(pad_l + slot * i)}" y="{_n(pad_t)}" width="{_n(slot)}" height="{_n(ph)}" fill="transparent"><title>{_e(tip)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))  # nosec B704 - text escaped via _e()


# ---- posterior interval rows (bandit arms) -------------------------------------------
def interval_rows(
    uid: str,
    title: str,
    desc: str,
    rows: Sequence[dict],
    *,
    width: float = 560,
    label_w: float = 150,
    row_h: float = 34,
) -> Markup:
    """One row per arm: 90% interval bar + posterior mean dot on a 0-100% axis.

    rows: [{label, mean, lo, hi, n, wins, lead:bool}]
    """
    if not rows:
        return Markup("")
    right_w = 64
    pad_t, pad_b = 8, 26
    height = pad_t + row_h * len(rows) + pad_b
    x0, x1 = label_w, width - right_w

    def X(p: float) -> float:
        return x0 + (x1 - x0) * max(0.0, min(1.0, p))

    out = [_svg_open(uid, width, height, title, desc)]
    axis_y = pad_t + row_h * len(rows)
    for p in (0, 0.25, 0.5, 0.75, 1):
        x = X(p)
        out.append(f'<line x1="{_n(x)}" x2="{_n(x)}" y1="{_n(pad_t)}" y2="{_n(axis_y)}" stroke="{AXIS if p == 0.5 else GRID}" stroke-width="1"/>')
        out.append(f'<text class="ax" x="{_n(x)}" y="{_n(axis_y + 18)}" text-anchor="middle" fill="{MUTED}">{int(p * 100)}%</text>')
    for k, r in enumerate(rows):
        cy = pad_t + row_h * k + row_h / 2
        tried = (r.get("n") or 0) > 0
        color = ACCENT if tried else BASELINE
        out.append(f'<text class="lbl" x="0" y="{_n(cy + 4)}" fill="{INK if r.get("lead") else INK2}">{_e(r["label"])}</text>')
        lo, hi = X(r["lo"]), X(r["hi"])
        out.append(f'<rect x="{_n(lo)}" y="{_n(cy - 4)}" width="{_n(max(hi - lo, 2))}" height="8" rx="4" fill="{color}" fill-opacity="{0.55 if r.get("lead") else 0.32}"/>')
        out.append(f'<circle cx="{_n(X(r["mean"]))}" cy="{_n(cy)}" r="5" fill="{color}" stroke="{SURFACE}" stroke-width="2"/>')
        out.append(f'<text class="ax" x="{_n(width)}" y="{_n(cy + 4)}" text-anchor="end" fill="{MUTED}">n {r.get("n", 0)}</text>')
        tip = (
            f"{r['label']}: mean {r['mean'] * 100:.0f}%, 90% interval {r['lo'] * 100:.0f}–{r['hi'] * 100:.0f}%, "
            f"n={r.get('n', 0)}, wins={r.get('wins', 0)}"
        )
        out.append(f'<rect class="hit" x="0" y="{_n(cy - row_h / 2)}" width="{_n(width)}" height="{_n(row_h)}" fill="transparent"><title>{_e(tip)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))  # nosec B704 - text escaped via _e()


# ---- strip plot (logged factors vs composite) ----------------------------------------
def strip_plot(
    uid: str,
    title: str,
    desc: str,
    rows: Sequence[dict],
    *,
    width: float = 560,
    label_w: float = 130,
    row_h: float = 32,
    x_fmt: Fmt = lambda v: f"{v:.2f}",
    ref: float | None = None,
    ref_label: str = "median",
) -> Markup:
    """rows: [{label, values:[float], mean, n, enough:bool}] - every reel a gray dot, the mean an accent tick."""
    allv = [v for r in rows for v in r["values"]]
    if not rows or not allv:
        return Markup("")
    lo, hi = min(allv), max(allv)
    ticks = nice_ticks(lo, hi, 4)
    t0, t1 = ticks[0], ticks[-1]
    right_w = 92
    pad_t, pad_b = 8, 26
    height = pad_t + row_h * len(rows) + pad_b
    x0, x1 = label_w, width - right_w

    def X(v: float) -> float:
        return x0 + (x1 - x0) * (v - t0) / (t1 - t0)

    out = [_svg_open(uid, width, height, title, desc)]
    axis_y = pad_t + row_h * len(rows)
    for t in ticks:
        x = X(t)
        out.append(f'<line x1="{_n(x)}" x2="{_n(x)}" y1="{_n(pad_t)}" y2="{_n(axis_y)}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text class="ax" x="{_n(x)}" y="{_n(axis_y + 18)}" text-anchor="middle" fill="{MUTED}">{_e(x_fmt(t))}</text>')
    if ref is not None:
        xr = X(ref)
        out.append(f'<line x1="{_n(xr)}" x2="{_n(xr)}" y1="{_n(pad_t)}" y2="{_n(axis_y)}" stroke="{BASELINE}" stroke-width="1"><title>{_e(ref_label)}: {_e(x_fmt(ref))}</title></line>')
    for k, r in enumerate(rows):
        cy = pad_t + row_h * k + row_h / 2
        out.append(f'<text class="lbl" x="0" y="{_n(cy + 4)}" fill="{INK2}">{_e(r["label"])}</text>')
        for v in r["values"]:
            out.append(f'<circle cx="{_n(X(v))}" cy="{_n(cy)}" r="3.5" fill="{BASELINE}" fill-opacity="0.75"/>')
        if r.get("mean") is not None:
            color = ACCENT if r.get("enough") else BASELINE
            xm = X(r["mean"])
            out.append(f'<rect x="{_n(xm - 2)}" y="{_n(cy - 9)}" width="4" height="18" rx="2" fill="{color}" stroke="{SURFACE}" stroke-width="1.5"/>')
            out.append(f'<text class="ax" x="{_n(width)}" y="{_n(cy + 4)}" text-anchor="end" fill="{INK2 if r.get("enough") else MUTED}">{_e(x_fmt(r["mean"]))} · n {r["n"]}</text>')
        tip = f"{r['label']}: mean {x_fmt(r['mean']) if r.get('mean') is not None else '–'}, n={r['n']}" + ("" if r.get("enough") else " (not enough data)")
        out.append(f'<rect class="hit" x="0" y="{_n(cy - row_h / 2)}" width="{_n(width)}" height="{_n(row_h)}" fill="transparent"><title>{_e(tip)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))  # nosec B704 - text escaped via _e()


# ---- tick status strip -------------------------------------------------------------
TICK_COLORS = {"ok": STATUS["good"], "error": STATUS["critical"], "skipped": NEUTRAL_STATE, "locked": NEUTRAL_STATE, "paused": STATUS["warning"]}


def status_strip(uid: str, title: str, desc: str, days: Sequence[dict], *, width: float = 1160, label_w: float = 92) -> Markup:
    """days: [{label, cells:[{status|None, future:bool, tip}] * 96}] - one row per local day."""
    if not days:
        return Markup("")
    slots = len(days[0]["cells"])
    cell_h, gap_y, pad_b = 18, 8, 22
    height = len(days) * (cell_h + gap_y) + pad_b
    cw = (width - label_w) / slots
    out = [_svg_open(uid, width, height, title, desc)]
    for k, day in enumerate(days):
        y = k * (cell_h + gap_y)
        out.append(f'<text class="ax" x="0" y="{_n(y + 13)}" fill="{INK2}">{_e(day["label"])}</text>')
        for i, c in enumerate(day["cells"]):
            if c.get("future"):
                continue
            fill = TICK_COLORS.get(c.get("status") or "", EMPTY_CELL)
            x = label_w + i * cw
            w = max(cw - (1 if cw > 3 else 0.4), 0.6)
            tip = c.get("tip") or ""
            out.append(f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{cell_h}" rx="{1.5 if cw > 5 else 0}" fill="{fill}"><title>{_e(tip)}</title></rect>')
    ay = len(days) * (cell_h + gap_y) + 12
    for h in (0, 6, 12, 18, 24):
        x = label_w + (slots * h / 24) * cw
        anchor = "start" if h == 0 else ("end" if h == 24 else "middle")
        out.append(f'<text class="ax" x="{_n(x)}" y="{_n(ay)}" text-anchor="{anchor}" fill="{MUTED}">{h:02d}:00</text>')
    out.append("</svg>")
    return Markup("".join(out))  # nosec B704 - text escaped via _e()


# ---- meter + sparkline ----------------------------------------------------------------
def meter(label: str, frac: float | None, severity: str = "good", *, width: float = 320) -> Markup:
    """A thin ratio bar; the value itself is printed next to it in the HTML."""
    f = 0.0 if frac is None else max(0.0, min(1.0, frac))
    color = {"accent": ACCENT, "none": BASELINE}.get(severity, STATUS.get(severity, ACCENT))
    fill = f'<rect x="0" y="0" width="{_n(max(width * f, 4 if f > 0 else 0))}" height="8" rx="4" fill="{color}"/>' if f > 0 else ""
    return Markup(  # nosec B704 - text escaped via _e()
        f'<svg class="meter" viewBox="0 0 {_n(width)} 8" width="{_n(width)}" height="8" role="img" aria-label="{_e(label)}" focusable="false">'
        f'<rect x="0" y="0" width="{_n(width)}" height="8" rx="4" fill="{EMPTY_CELL}"/>{fill}</svg>'
    )


def sparkline(values: Sequence[float | None], *, label: str, width: float = 120, height: float = 32) -> Markup:
    """De-emphasis gray line with the current point in the accent."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return Markup("")
    lo = min(v for _, v in pts)
    hi = max(v for _, v in pts)
    span = (hi - lo) or 1
    n = len(values)

    def X(i: int) -> float:
        return 3 + (width - 8) * i / (n - 1)

    def Y(v: float) -> float:
        return 4 + (height - 8) * (1 - (v - lo) / span)

    d = "M" + " L".join(f"{_n(X(i))},{_n(Y(v))}" for i, v in pts)
    li, lv = pts[-1]
    return Markup(  # nosec B704 - text escaped via _e()
        f'<svg class="spark" viewBox="0 0 {_n(width)} {_n(height)}" width="{_n(width)}" height="{_n(height)}" role="img" aria-label="{_e(label)}" focusable="false">'
        f'<path d="{d}" fill="none" stroke="{BASELINE}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{_n(X(li))}" cy="{_n(Y(lv))}" r="3.5" fill="{ACCENT}" stroke="{SURFACE}" stroke-width="1.5"/></svg>'
    )


def dual(fn: Callable[..., Markup], uid: str, *args, wide: float, narrow: float, **kw) -> Markup:
    """Render a chart twice (desktop + phone width) so SVG text stays ~12px on both.

    CSS shows one variant per breakpoint; the hidden one is display:none (not read by AT).
    """
    a = fn(uid + "-w", *args, width=wide, **kw)
    if not a:
        return Markup("")
    nkw = dict(kw)
    if "label_w" in nkw:
        nkw["label_w"] = min(nkw["label_w"], narrow * 0.36)
    b = fn(uid + "-n", *args, width=narrow, **nkw)
    return Markup(  # nosec B704 - text escaped via _e()
        '<div class="chart-v">'
        + str(a).replace('class="chart"', 'class="chart chart-wide"', 1)
        + str(b).replace('class="chart"', 'class="chart chart-narrow"', 1)
        + "</div>"
    )
