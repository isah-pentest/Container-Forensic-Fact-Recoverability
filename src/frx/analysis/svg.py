"""Minimal dependency-free SVG drawing for the publication figures (no plotting library is pinned
in the frozen environment, so figures are written as plain SVG from the analysis outputs)."""
from __future__ import annotations

from xml.sax.saxutils import escape

INK, MUTED, GRID, ACCENT, ACCENT2, BAD = "#1f2933", "#616e7c", "#d9dee4", "#2f6fb0", "#c07a1d", "#b23b3b"
FONT = "font-family='Helvetica, Arial, sans-serif'"


class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.items = w, h, []

    def line(self, x1, y1, x2, y2, color=INK, width=1.0, dash: str | None = None):
        d = f" stroke-dasharray='{dash}'" if dash else ""
        self.items.append(f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='{color}' "
                          f"stroke-width='{width}'{d}/>")

    def rect(self, x, y, w, h, fill="none", stroke=INK, width=1.0, rx=0):
        self.items.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{w:.1f}' height='{h:.1f}' rx='{rx}' "
                          f"fill='{fill}' stroke='{stroke}' stroke-width='{width}'/>")

    def circle(self, x, y, r=4, fill=ACCENT, stroke="none"):
        self.items.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{r}' fill='{fill}' stroke='{stroke}'/>")

    def text(self, x, y, s, size=12, anchor="start", color=INK, weight="normal", rotate: float | None = None):
        tr = f" transform='rotate({rotate} {x:.1f} {y:.1f})'" if rotate is not None else ""
        self.items.append(f"<text x='{x:.1f}' y='{y:.1f}' font-size='{size}' text-anchor='{anchor}' fill='{color}' "
                          f"font-weight='{weight}' {FONT}{tr}>{escape(str(s))}</text>")

    def arrow(self, x1, y1, x2, y2, color=INK):
        self.line(x1, y1, x2, y2, color, 1.2)
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        for da in (2.6, -2.6):
            self.line(x2, y2, x2 + 7 * math.cos(a + da), y2 + 7 * math.sin(a + da), color, 1.2)

    def render(self) -> str:
        body = "\n".join(self.items)
        return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{self.w}' height='{self.h}' "
                f"viewBox='0 0 {self.w} {self.h}'>\n<rect width='100%' height='100%' fill='white'/>\n{body}\n</svg>\n")


class Axis:
    """Linear map from data to pixels with ticks."""

    def __init__(self, lo, hi, p0, p1):
        self.lo, self.hi, self.p0, self.p1 = lo, hi, p0, p1

    def __call__(self, v):
        return self.p0 + (v - self.lo) / (self.hi - self.lo) * (self.p1 - self.p0)


def y_axis(c: Canvas, ax: Axis, x: float, ticks, fmt="{:.1f}", label: str | None = None, grid_to: float | None = None):
    c.line(x, ax.p0, x, ax.p1, MUTED)
    for t in ticks:
        y = ax(t)
        c.line(x - 4, y, x, y, MUTED)
        c.text(x - 7, y + 4, fmt.format(t), 11, "end", MUTED)
        if grid_to is not None:
            c.line(x, y, grid_to, y, GRID, 0.8)
    if label:
        c.text(x - 44, (ax.p0 + ax.p1) / 2, label, 12, "middle", INK, rotate=-90)


def x_axis(c: Canvas, ax: Axis, y: float, ticks, fmt="{:.1f}", label: str | None = None, grid_to: float | None = None):
    c.line(ax.p0, y, ax.p1, y, MUTED)
    for t in ticks:
        x = ax(t)
        c.line(x, y, x, y + 4, MUTED)
        c.text(x, y + 17, fmt.format(t), 11, "middle", MUTED)
        if grid_to is not None:
            c.line(x, y, x, grid_to, GRID, 0.8)
    if label:
        c.text((ax.p0 + ax.p1) / 2, y + 36, label, 12, "middle")


def shade(p: float) -> str:
    """Sequential blue for proportions 0..1 (one hue, lightness carries the value)."""
    p = max(0.0, min(1.0, p))
    lo, hi = (238, 243, 249), (31, 84, 150)
    r, g, b = (int(lo[i] + (hi[i] - lo[i]) * p) for i in range(3))
    return f"rgb({r},{g},{b})"
