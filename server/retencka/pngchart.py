"""Small PNG line chart (standard library only) – the level chart sent by the Telegram bot.

No matplotlib on a Pi Zero: an RGB canvas, a 3x5 pixel font for the axis labels
(digits, %, ., :, space) and a hand-written PNG encoder (zlib + CRC chunks).
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime
from typing import Optional, Sequence

# 3x5 glyphs, rows top->bottom, 3 bits per row (MSB = left)
_FONT = {
    "0": (7, 5, 5, 5, 7), "1": (2, 6, 2, 2, 7), "2": (7, 1, 7, 4, 7), "3": (7, 1, 7, 1, 7),
    "4": (5, 5, 7, 1, 1), "5": (7, 4, 7, 1, 7), "6": (7, 4, 7, 5, 7), "7": (7, 1, 1, 1, 1),
    "8": (7, 5, 7, 5, 7), "9": (7, 5, 7, 1, 7), "%": (5, 1, 2, 4, 5), ".": (0, 0, 0, 0, 2),
    ":": (0, 2, 0, 2, 0), " ": (0, 0, 0, 0, 0), "-": (0, 0, 7, 0, 0),
}

BG = (15, 23, 42)
GRID = (39, 51, 72)
TEXT = (148, 163, 184)
LINE = (56, 189, 248)
FILL = (56, 189, 248)
BANDS = {"refill": (16, 185, 129), "rain": (56, 189, 248)}


class Canvas:
    def __init__(self, w: int, h: int, bg=BG):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def set(self, x: int, y: int, c) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def blend(self, x: int, y: int, c, a: float) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            for k in range(3):
                self.px[i + k] = int(self.px[i + k] * (1 - a) + c[k] * a)

    def hline(self, x0: int, x1: int, y: int, c) -> None:
        for x in range(max(0, x0), min(self.w, x1 + 1)):
            self.set(x, y, c)

    def vline(self, x: int, y0: int, y1: int, c, dash: int = 0) -> None:
        for y in range(max(0, y0), min(self.h, y1 + 1)):
            if not dash or (y // dash) % 2 == 0:
                self.set(x, y, c)

    def line(self, x0: int, y0: int, x1: int, y1: int, c, width: int = 2) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            for ox in range(width):
                for oy in range(width):
                    self.set(x0 + ox, y0 + oy, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def text(self, x: int, y: int, s: str, c=TEXT, scale: int = 2) -> None:
        for ch in s:
            rows = _FONT.get(ch, _FONT[" "])
            for ry, bits in enumerate(rows):
                for rx in range(3):
                    if bits & (4 >> rx):
                        for oy in range(scale):
                            for ox in range(scale):
                                self.set(x + rx * scale + ox, y + ry * scale + oy, c)
            x += 4 * scale

    @staticmethod
    def text_width(s: str, scale: int = 2) -> int:
        return len(s) * 4 * scale - scale

    def png(self) -> bytes:
        raw = bytearray()
        row = self.w * 3
        for y in range(self.h):
            raw.append(0)                       # filter: none
            raw += self.px[y * row:(y + 1) * row]

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))


def level_chart(t_ms: Sequence[int], pct: Sequence[Optional[float]], t_from: int, t_to: int,
                bands: Sequence[dict] = (), w: int = 800, h: int = 400) -> bytes:
    """Level (0-100 %) over time as PNG. t_ms in milliseconds, t_from/t_to in seconds."""
    c = Canvas(w, h)
    left, right, top, bottom = 64, w - 16, 16, h - 36
    span = max(1, t_to - t_from)

    def px(ts_ms: float) -> int:
        return left + int((ts_ms / 1000.0 - t_from) / span * (right - left))

    def py(p: float) -> int:
        return bottom - int(max(0.0, min(100.0, p)) / 100.0 * (bottom - top))

    for b in bands:                              # refill / rain intervals
        col = BANDS.get(b.get("kind"), BANDS["rain"])
        x0, x1 = max(left, px(b["start"])), min(right, px(b["end"]))
        if x1 < left or x0 > right:
            continue
        x1 = max(x1, x0 + 2)
        for x in range(x0, x1 + 1):
            for y in range(top, bottom + 1):
                c.blend(x, y, col, 0.12)
        c.vline(x0, top, bottom, col, dash=4)
        c.vline(x1, top, bottom, col, dash=4)

    for p in (0, 25, 50, 75, 100):               # horizontal grid + y labels
        y = py(p)
        c.hline(left, right, y, GRID)
        lbl = str(p)                              # unit (%) is in the caption
        c.text(left - 8 - Canvas.text_width(lbl), y - 5, lbl)

    day = datetime.fromtimestamp(t_from).replace(hour=0, minute=0, second=0, microsecond=0)
    step = 1 if span <= 8 * 86400 else 7
    while day.timestamp() <= t_to:               # vertical grid at local midnight + date labels
        ts = day.timestamp()
        if ts >= t_from:
            x = px(ts * 1000)
            c.vline(x, top, bottom, GRID)
            lbl = f"{day.day}.{day.month}."
            c.text(x - Canvas.text_width(lbl) // 2, bottom + 12, lbl)
        day = datetime.fromtimestamp(ts + step * 86400 + 7200).replace(hour=0, minute=0, second=0, microsecond=0)

    pts = [(px(t), py(v)) for t, v in zip(t_ms, pct) if v is not None]
    segs: list[list[tuple[int, int]]] = [[]]
    for (t, v) in zip(t_ms, pct):                # gaps (None) break the line
        if v is None:
            if segs[-1]:
                segs.append([])
        else:
            segs[-1].append((px(t), py(v)))
    top_of: dict[int, int] = {}                  # soft fill under the curve, once per column
    for seg in segs:
        for (x0, y0), (x1, y1) in zip(seg, seg[1:]):
            for x in range(x0, x1 + 1):
                yy = y0 + (y1 - y0) * (x - x0) // max(1, x1 - x0)
                top_of[x] = min(top_of.get(x, bottom), yy)
    for x, yy in top_of.items():
        for y in range(yy, bottom):
            c.blend(x, y, FILL, 0.10)
    for seg in segs:
        for (x0, y0), (x1, y1) in zip(seg, seg[1:]):
            c.line(x0, y0, x1, y1, LINE, 2)
    if pts:
        x, y = pts[-1]
        for oy in range(-3, 4):
            for ox in range(-3, 4):
                if ox * ox + oy * oy <= 9:
                    c.set(x + ox, y + oy, LINE)
    return c.png()
