"""Dependency-free PNG chart rendering (offline, guardrail #12).

The frontend bundles its own canvas chart module (static/chart.js) for the
interactive single-run / compare charts -- no CDN, no runtime network. For the
export zip's PNG files we render here, on the server, with a tiny pure-Python
PNG writer so the package needs neither Node nor matplotlib for charts. The
result is a valid PNG (RGBA, zlib) with axes, grid, a plotted line and points.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import Any

# A compact 5x7 bitmap font for the ASCII subset we use in labels/ticks.
_FONT: dict[str, list[str]] = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    " ": ["00000"] * 7,
    "e": ["00000", "01110", "10001", "11111", "10000", "10001", "01110"],
    "x": ["00000", "00000", "10001", "01010", "00100", "01010", "10001"],
    "p": ["00000", "11110", "10001", "11110", "10000", "10000", "10000"],
    "s": ["00000", "01111", "10000", "01110", "00001", "00001", "11110"],
    "/": ["00001", "00001", "00010", "00100", "01000", "10000", "10000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "c": ["00000", "01110", "10001", "10000", "10000", "10001", "01110"],
    "b": ["10000", "10000", "11110", "10001", "10001", "10001", "11110"],
    "t": ["00100", "00100", "11111", "00100", "00100", "00100", "00111"],
    "r": ["00000", "10110", "11001", "10000", "10000", "10000", "10000"],
    "q": ["00000", "01110", "10001", "10001", "01111", "00001", "00001"],
    "m": ["00000", "11010", "10101", "10101", "10101", "10101", "10101"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "9_": ["00000"] * 7,
    "_": ["00000", "00000", "00000", "00000", "00000", "00000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
}


class _Canvas:
    def __init__(self, width: int, height: int, bg: tuple[int, int, int, int]):
        self.w = width
        self.h = height
        self.px = [bytearray(bg) for _ in range(width * height)]  # flattened rows

    def set(self, x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y * self.w + x] = bytearray(color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.set(x, y, color)

    def line(self, x0: int, y0: int, x1: int, y1: int, color, width: int = 1) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for wy in range(-(width // 2), width // 2 + 1):
                for wx in range(-(width // 2), width // 2 + 1):
                    self.set(x0 + wx, y0 + wy, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def dot(self, cx: int, cy: int, color, r: int = 2) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self.set(x, y, color)

    def text(self, x: int, y: int, s: str, color, scale: int = 1) -> None:
        cx = x
        for ch in s.lower() if False else s:
            glyph = _FONT.get(ch) or _FONT.get(ch.lower()) or _FONT[" "]
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(
                            cx + gx * scale,
                            y + gy * scale,
                            cx + gx * scale + scale - 1,
                            y + gy * scale + scale - 1,
                            color,
                        )
            cx += 6 * scale

    def png(self) -> bytes:
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)  # filter type 0
            for x in range(self.w):
                raw.extend(self.px[y * self.w + x])

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b"")
        )


_BG = (250, 251, 253, 255)
_GRID = (224, 228, 235, 255)
_AXIS = (120, 130, 145, 255)
_TEXT = (55, 65, 81, 255)
_LINE = (37, 99, 235, 255)
_DOT = (220, 38, 38, 255)


def _fmt(value: float) -> str:
    a = abs(value)
    if a >= 1000:
        return f"{value:.0f}"
    if a >= 10:
        return f"{value:.1f}"
    if a >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def render_line_chart(
    points: list[tuple[float, float]],
    *,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    width: int = 640,
    height: int = 380,
) -> bytes:
    """Render a line chart as PNG bytes. ``points`` are (x, y) pairs."""
    pad_l, pad_r, pad_t, pad_b = 62, 20, 40, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    c = _Canvas(width, height, _BG)
    c.rect(pad_l, pad_t, pad_l + plot_w, pad_t + plot_h, (255, 255, 255, 255))
    # frame
    c.line(pad_l, pad_t, pad_l, pad_t + plot_h, _AXIS)
    c.line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, _AXIS)

    if title:
        c.text(pad_l, 12, title[:60], _TEXT, 1)

    numeric = [(float(x), float(y)) for x, y in points if y is not None]
    if not numeric:
        c.text(pad_l + 10, pad_t + 10, "no data", _AXIS, 2)
        return c.png()
    xs = [p[0] for p in numeric]
    ys = [p[1] for p in numeric]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmin == xmax:
        xmin -= 1
        xmax += 1
    if ymin == ymax:
        ymin -= abs(ymin) * 0.1 + 1
        ymax += abs(ymax) * 0.1 + 1
    ypad = (ymax - ymin) * 0.1
    ymin -= ypad
    ymax += ypad

    # horizontal grid + y ticks
    for i in range(5):
        gy = pad_t + int(plot_h * i / 4)
        c.line(pad_l, gy, pad_l + plot_w, gy, _GRID)
        value = ymax - (ymax - ymin) * i / 4
        c.text(4, gy - 3, _fmt(value), _TEXT, 1)
    # x ticks
    for i in range(5):
        gx = pad_l + int(plot_w * i / 4)
        c.line(gx, pad_t + plot_h, gx, pad_t + plot_h + 3, _AXIS)
        value = xmin + (xmax - xmin) * i / 4
        c.text(gx - 10, pad_t + plot_h + 6, _fmt(value), _TEXT, 1)

    def to_px(x: float, y: float) -> tuple[int, int]:
        px = pad_l + int((x - xmin) / (xmax - xmin) * plot_w)
        py = pad_t + plot_h - int((y - ymin) / (ymax - ymin) * plot_h)
        return px, py

    coords = [to_px(x, y) for x, y in numeric]
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        c.line(x0, y0, x1, y1, _LINE, 2)
    for x0, y0 in coords:
        c.dot(x0, y0, _DOT, 2)
    if y_label:
        c.text(pad_l, pad_t + plot_h + 20, y_label[:40], _TEXT, 1)
    if x_label:
        c.text(pad_l + plot_w - 90, height - 12, x_label[:15], _TEXT, 1)
    return c.png()


def render_from_chart_payload(payload: dict[str, Any]) -> bytes:
    """Turn a chart_data() payload into a PNG (used by export)."""
    label = payload.get("label", "")
    unit = payload.get("unit", "")
    if payload.get("mode") == "single":
        for series in payload.get("series", []):
            pts = [(p["x"], p["y"]) for p in series.get("points", []) if p.get("y") is not None]
            return render_line_chart(pts, title=label, y_label=unit, x_label="request #")
        return render_line_chart([], title=label)
    pts = [
        (p["x"] if isinstance(p["x"], (int, float)) else i, p["y"])
        for i, p in enumerate(payload.get("points", []))
        if p.get("y") is not None
    ]
    # non-numeric x -> use index
    pts = [(float(x) if isinstance(x, (int, float)) else float(i), y) for i, (x, y) in enumerate(pts)]
    return render_line_chart(pts, title=label, y_label=unit, x_label=str(payload.get("x_field", "")))
