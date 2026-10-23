"""Dependency-free, deterministic PNG charts for Warcraft Logs trends."""
from __future__ import annotations

import binascii
import math
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class ChartDataError(ValueError):
    """Raised when chart input is unsafe or cannot be rendered."""


@dataclass(frozen=True)
class TrendPoint:
    """One dated metric used by :func:`render_dps_trend_png`."""

    date: str
    value: float


_FONT = {
    " ": ("000", "000", "000", "000", "000", "000", "000"),
    "-": ("000", "000", "000", "111", "000", "000", "000"),
    ".": ("000", "000", "000", "000", "000", "010", "010"),
    ":": ("000", "010", "010", "000", "010", "010", "000"),
    "/": ("001", "001", "010", "010", "100", "100", "000"),
    "%": ("10001", "00010", "00100", "01000", "10001", "00000", "00000"),
    "?": ("111", "001", "001", "010", "010", "000", "010"),
    "0": ("111", "101", "101", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "010", "010", "111"),
    "2": ("111", "001", "001", "111", "100", "100", "111"),
    "3": ("111", "001", "001", "111", "001", "001", "111"),
    "4": ("101", "101", "101", "111", "001", "001", "001"),
    "5": ("111", "100", "100", "111", "001", "001", "111"),
    "6": ("111", "100", "100", "111", "101", "101", "111"),
    "7": ("111", "001", "001", "010", "010", "010", "010"),
    "8": ("111", "101", "101", "111", "101", "101", "111"),
    "9": ("111", "101", "101", "111", "001", "001", "111"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("111", "010", "010", "010", "010", "010", "111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


class _Canvas:
    def __init__(self, width, height, color):
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(color) * width * height)

    def pixel(self, x, y, color):
        x, y = int(x), int(y)
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset:offset + 3] = bytes(color)

    def rectangle(self, left, top, right, bottom, color):
        left, right = max(0, int(left)), min(self.width - 1, int(right))
        top, bottom = max(0, int(top)), min(self.height - 1, int(bottom))
        if left > right or top > bottom:
            return
        row = bytes(color) * (right - left + 1)
        for y in range(top, bottom + 1):
            offset = (y * self.width + left) * 3
            self.pixels[offset:offset + len(row)] = row

    def line(self, x0, y0, x1, y1, color, thickness=1):
        x0, y0, x1, y1 = map(int, (x0, y0, x1, y1))
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        error = dx + dy
        radius = max(0, int(thickness) // 2)
        while True:
            self.rectangle(x0 - radius, y0 - radius, x0 + radius, y0 + radius, color)
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy

    def circle(self, center_x, center_y, radius, fill, outline):
        center_x, center_y, radius = int(center_x), int(center_y), int(radius)
        inner = max(0, radius - 2) ** 2
        outer = radius ** 2
        for y in range(center_y - radius, center_y + radius + 1):
            for x in range(center_x - radius, center_x + radius + 1):
                distance = (x - center_x) ** 2 + (y - center_y) ** 2
                if distance <= outer:
                    self.pixel(x, y, fill if distance <= inner else outline)

    def text(self, value, x, y, color, *, scale=1, align="left"):
        rendered = _ascii_text(value)
        widths = [len(_FONT.get(character, _FONT["?"])[0]) for character in rendered]
        total_width = sum(widths) * scale + max(0, len(widths) - 1) * scale
        if align == "center":
            x -= total_width // 2
        elif align == "right":
            x -= total_width
        for character, width in zip(rendered, widths):
            glyph = _FONT.get(character, _FONT["?"])
            for row_index, row in enumerate(glyph):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.rectangle(
                            x + column_index * scale,
                            y + row_index * scale,
                            x + (column_index + 1) * scale - 1,
                            y + (row_index + 1) * scale - 1,
                            color,
                        )
            x += (width + 1) * scale


def _ascii_text(value):
    return "".join(
        character if character in _FONT else "?"
        for character in str(value or "").upper()
        if character >= " "
    )


def _clean_text(value, *, maximum, field):
    rendered = "".join(character for character in str(value or "") if character >= " ").strip()
    if not rendered:
        raise ChartDataError(f"{field}_required")
    if len(rendered) > maximum:
        raise ChartDataError(f"{field}_too_long")
    return rendered


def _coerce_points(points):
    if isinstance(points, (str, bytes)) or not isinstance(points, Sequence):
        raise ChartDataError("invalid_points")
    if not 1 <= len(points) <= 25:
        raise ChartDataError("point_count_out_of_range")
    normalized = []
    for item in points:
        if isinstance(item, TrendPoint):
            date, value = item.date, item.value
        elif isinstance(item, Mapping):
            date = item.get("date")
            value = item.get("dps", item.get("value"))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
            date, value = item
        else:
            raise ChartDataError("invalid_point")
        date = _clean_text(date, maximum=32, field="date")
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value)) or float(value) < 0):
            raise ChartDataError("invalid_value")
        normalized.append(TrendPoint(date, float(value)))
    return tuple(normalized)


def _metric(value):
    value = float(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            precision = 0 if scaled >= 100 else (1 if scaled >= 10 else 2)
            return f"{scaled:.{precision}f}{suffix}"
    return f"{value:.0f}"


def _png_chunk(kind, data):
    payload = kind + data
    checksum = binascii.crc32(payload) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", checksum)


def _encode_png(canvas, title):
    rows = bytearray()
    stride = canvas.width * 3
    for y in range(canvas.height):
        rows.append(0)
        offset = y * stride
        rows.extend(canvas.pixels[offset:offset + stride])
    header = struct.pack(">IIBBBBB", canvas.width, canvas.height, 8, 2, 0, 0, 0)
    metadata = b"Title\x00" + _ascii_text(title).encode("ascii", "replace")[:160]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", metadata)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def render_dps_trend_png(points, *, title="Historical DPS", width=900, height=450):
    """Render dated DPS values as a deterministic RGB PNG.

    ``points`` may contain :class:`TrendPoint` values, ``(date, value)`` pairs,
    or mappings with ``date`` and either ``dps`` or ``value``. Between one and
    25 finite, non-negative points are accepted. Labels and dimensions are
    bounded so API or Discord data cannot create an unbounded allocation.
    """
    title = _clean_text(title, maximum=120, field="title")
    points = _coerce_points(points)
    if (not isinstance(width, int) or isinstance(width, bool)
            or not isinstance(height, int) or isinstance(height, bool)
            or not 480 <= width <= 1600 or not 300 <= height <= 1000):
        raise ChartDataError("invalid_image_size")

    background = (13, 20, 32)
    panel = (20, 31, 48)
    grid = (49, 65, 88)
    axis = (117, 139, 166)
    text = (226, 232, 240)
    muted = (159, 176, 198)
    trend = (72, 184, 255)
    point_fill = (251, 191, 36)

    canvas = _Canvas(width, height, background)
    canvas.text(title[:60], 28, 22, text, scale=2)
    left, top, right, bottom = 82, 66, width - 30, height - 72
    canvas.rectangle(left, top, right, bottom, panel)

    values = [point.value for point in points]
    low, high = min(values), max(values)
    if high == low:
        padding = max(1.0, high * 0.05)
    else:
        padding = max(1.0, (high - low) * 0.12)
    y_min = max(0.0, low - padding)
    y_max = high + padding
    if y_max <= y_min:
        y_max = y_min + 1.0

    for index in range(5):
        y = top + (bottom - top) * index / 4
        canvas.line(left, y, right, y, grid)
        value = y_max - (y_max - y_min) * index / 4
        canvas.text(_metric(value), left - 8, y - 3, muted, align="right")
    canvas.line(left, top, left, bottom, axis, thickness=2)
    canvas.line(left, bottom, right, bottom, axis, thickness=2)

    coordinates = []
    for index, point in enumerate(points):
        x = (left + (right - left) / 2 if len(points) == 1
             else left + (right - left) * index / (len(points) - 1))
        y = bottom - (point.value - y_min) * (bottom - top) / (y_max - y_min)
        coordinates.append((x, y))
    for start, end in zip(coordinates, coordinates[1:]):
        canvas.line(*start, *end, trend, thickness=3)
    for index, ((x, y), point) in enumerate(zip(coordinates, points)):
        canvas.circle(x, y, 6, point_fill, (255, 255, 255))
        if len(points) <= 8 or index in {0, len(points) - 1}:
            canvas.text(_metric(point.value), x, max(top + 5, y - 18), text, align="center")

    if len(points) <= 8:
        label_indexes = range(len(points))
    else:
        label_indexes = sorted({0, len(points) // 2, len(points) - 1})
    for index in label_indexes:
        x, _ = coordinates[index]
        canvas.text(points[index].date[:12], x, bottom + 14, muted, align="center")

    return _encode_png(canvas, title)
