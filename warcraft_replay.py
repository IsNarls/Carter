"""Safe, deterministic Warcraft Logs positional snapshots for Discord."""
from __future__ import annotations

import binascii
import math
import re
import struct
import zlib
from collections import Counter


class ReplayDataError(RuntimeError):
    """A credential-free reason a replay snapshot could not be built."""


_TIMESTAMP = re.compile(
    r"(?<![\d:])(?:(?P<hours>\d{1,2}):(?P<hour_minutes>[0-5]\d):"
    r"(?P<hour_seconds>[0-5]\d)|(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d))"
    r"(?:\.(?P<fraction>\d{1,3}))?(?![\d:])"
)
_POSITION = re.compile(
    r"(?<![A-Za-z0-9.-])(?:https?://)?(?:[A-Za-z0-9-]+\.)*"
    r"warcraftlogs\.com/reports/"
    r"[A-Za-z0-9]{8,32}[^\s<>]*[?#&]position=(\d+)",
    re.IGNORECASE,
)
_REPORT_CODE = re.compile(r"[A-Za-z0-9]{8,32}")


def parse_fight_timestamp(text):
    """Return one unambiguous fight-relative timestamp in milliseconds.

    Clock-looking strings are accepted only with nearby fight-time context so a
    wall-clock time or an ISO timestamp does not silently select a replay frame.
    A Warcraft Logs replay URL's ``position`` parameter is already defined in
    fight-relative milliseconds and is accepted directly.
    """
    text = str(text or "")
    positions = {int(match.group(1)) for match in _POSITION.finditer(text)}
    if len(positions) > 1:
        return None
    found = set(positions)
    for match in _TIMESTAMP.finditer(text):
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():min(len(text), match.end() + 45)]
        nearby = (before[-20:] + after[:20]).casefold()
        if re.search(r"\b(?:am|pm|utc|gmt)\b", nearby):
            continue
        prefix_context = re.search(
            r"(?:\b(?:at|around|near|timestamp|time)\b|@)"
            r"(?:\s+(?:about|roughly|exactly))?\s*[:=\-]?\s*$",
            before,
            re.IGNORECASE,
        )
        suffix_context = re.match(
            r"\s*(?:mark\b|(?:into|in)\s+(?:the\s+)?(?:fight|pull|attempt)\b)",
            after,
            re.IGNORECASE,
        )
        if not prefix_context and not suffix_context:
            continue
        if match.group("hours") is not None:
            seconds = (
                int(match.group("hours")) * 3600
                + int(match.group("hour_minutes")) * 60
                + int(match.group("hour_seconds"))
            )
        else:
            seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
        fraction = (match.group("fraction") or "").ljust(3, "0")
        found.add(seconds * 1000 + (int(fraction) if fraction else 0))
    return next(iter(found)) if len(found) == 1 else None


def format_fight_timestamp(milliseconds):
    milliseconds = max(0, int(milliseconds))
    seconds, fraction = divmod(milliseconds, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    clock = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    return f"{clock}.{fraction:03d}" if fraction else clock


def replay_url(report_code, fight_id, offset_ms):
    code = str(report_code or "")
    if not _REPORT_CODE.fullmatch(code):
        raise ReplayDataError("invalid_report")
    try:
        fight_id, offset_ms = int(fight_id), int(offset_ms)
    except (TypeError, ValueError):
        raise ReplayDataError("invalid_timestamp") from None
    if fight_id <= 0 or offset_ms < 0:
        raise ReplayDataError("invalid_timestamp")
    return (
        f"https://www.warcraftlogs.com/reports/{code}"
        f"#fight={fight_id}&view=replay&position={offset_ms}"
    )


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _actor_id(value):
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _clean_label(value, fallback):
    rendered = "".join(character for character in str(value or "") if character >= " ")
    return (rendered.strip() or fallback)[:80]


def _normalized_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _bounds_from_positions(positions):
    xs = [float(item["x"]) for item in positions]
    ys = [float(item["y"]) for item in positions]
    if not xs or not ys:
        raise ReplayDataError("positions_unavailable")
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    span_x, span_y = max_x - min_x, max_y - min_y
    pad_x = max(100.0, span_x * 0.12)
    pad_y = max(100.0, span_y * 0.12)
    return {
        "minX": min_x - pad_x,
        "maxX": max_x + pad_x,
        "minY": min_y - pad_y,
        "maxY": max_y + pad_y,
    }


def _valid_bounds(value):
    if not isinstance(value, dict):
        return None
    keys = ("minX", "maxX", "minY", "maxY")
    if not all(_finite_number(value.get(key)) for key in keys):
        return None
    bounds = {key: float(value[key]) for key in keys}
    if bounds["maxX"] <= bounds["minX"] or bounds["maxY"] <= bounds["minY"]:
        return None
    return bounds


def build_replay_snapshot(report, fight_id, offset_ms, events):
    """Normalize bounded positional events into a renderer-safe snapshot."""
    if not isinstance(report, dict):
        raise ReplayDataError("report_unavailable")
    try:
        fight_id, offset_ms = int(fight_id), int(offset_ms)
    except (TypeError, ValueError):
        raise ReplayDataError("invalid_timestamp") from None
    fight = next(
        (item for item in report.get("fights") or []
         if isinstance(item, dict) and item.get("id") == fight_id),
        None,
    )
    if fight is None:
        raise ReplayDataError("fight_unavailable")
    start, end = fight.get("startTime"), fight.get("endTime")
    if not _finite_number(start) or not _finite_number(end) or float(end) <= float(start):
        raise ReplayDataError("fight_unavailable")
    duration_ms = int(float(end) - float(start))
    if offset_ms < 0 or offset_ms > duration_ms:
        raise ReplayDataError("timestamp_out_of_range")
    target_time = float(start) + offset_ms

    actors = {}
    for actor in ((report.get("masterData") or {}).get("actors") or []):
        if not isinstance(actor, dict):
            continue
        actor_id = _actor_id(actor.get("id"))
        if actor_id:
            actors[actor_id] = actor
    friendly_ids = {
        actor_id for actor_id in (_actor_id(value) for value in fight.get("friendlyPlayers") or [])
        if actor_id
    }
    newest, samples = {}, Counter()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        resource_actor = event.get("resourceActor")
        owner = event.get("sourceID") if resource_actor == 1 else (
            event.get("targetID") if resource_actor == 2 else None
        )
        owner = _actor_id(owner)
        timestamp = event.get("timestamp")
        if (not owner or not _finite_number(timestamp) or float(timestamp) > target_time
                or not _finite_number(event.get("x")) or not _finite_number(event.get("y"))):
            continue
        samples[owner] += 1
        previous = newest.get(owner)
        if previous is None or float(timestamp) >= float(previous.get("timestamp") or 0):
            newest[owner] = event
    if not newest:
        raise ReplayDataError("positions_unavailable")

    friendly_maps = Counter(
        int(event["mapID"]) for actor_id, event in newest.items()
        if actor_id in friendly_ids and _finite_number(event.get("mapID"))
    )
    all_maps = Counter(
        int(event["mapID"]) for event in newest.values() if _finite_number(event.get("mapID"))
    )
    map_id = (friendly_maps or all_maps).most_common(1)[0][0] if (friendly_maps or all_maps) else None
    positions = []
    for actor_id, event in newest.items():
        event_map = int(event["mapID"]) if _finite_number(event.get("mapID")) else None
        if map_id is not None and event_map is not None and event_map != map_id:
            continue
        actor = actors.get(actor_id) or {}
        hit_points = event.get("hitPoints") if _finite_number(event.get("hitPoints")) else None
        max_hit_points = (
            event.get("maxHitPoints") if _finite_number(event.get("maxHitPoints")) else None
        )
        positions.append({
            "actor_id": actor_id,
            "name": _clean_label(actor.get("name"), f"Actor {actor_id}"),
            "type": _clean_label(actor.get("type"), "Unknown"),
            "sub_type": _clean_label(actor.get("subType"), "Unknown"),
            "x": float(event["x"]),
            "y": float(event["y"]),
            "facing": float(event["facing"]) if _finite_number(event.get("facing")) else None,
            "hit_points": float(hit_points) if hit_points is not None else None,
            "max_hit_points": float(max_hit_points) if max_hit_points is not None else None,
            "age_ms": max(0, int(target_time - float(event["timestamp"]))),
            "is_friendly": actor_id in friendly_ids,
            "sample_count": samples[actor_id],
        })

    encounter_name = _clean_label(fight.get("name"), "Unknown encounter")
    friendlies = sorted(
        (item for item in positions if item["is_friendly"]),
        key=lambda item: (item["name"].casefold(), item["actor_id"]),
    )
    hostiles = [item for item in positions if not item["is_friendly"]]
    encounter_key = _normalized_name(encounter_name)
    exact_bosses = [item for item in hostiles if _normalized_name(item["name"]) == encounter_key]
    other_hostiles = [item for item in hostiles if item not in exact_bosses]
    other_hostiles.sort(key=lambda item: (-item["sample_count"], item["name"].casefold()))
    selected = (friendlies + exact_bosses + other_hostiles[:6])[:40]
    if not selected:
        raise ReplayDataError("positions_unavailable")
    exact_ids = {item["actor_id"] for item in exact_bosses}
    for index, item in enumerate(selected, 1):
        item["marker"] = index
        item["is_boss"] = item["actor_id"] in exact_ids

    bounds = _valid_bounds(fight.get("boundingBox")) or _bounds_from_positions(selected)
    positioned_friendlies = {item["actor_id"] for item in selected if item["is_friendly"]}
    return {
        "fight_id": fight_id,
        "fight_name": encounter_name,
        "offset_ms": offset_ms,
        "duration_ms": duration_ms,
        "map_id": map_id,
        "bounds": bounds,
        "positions": selected,
        "friendly_total": len(friendly_ids),
        "friendly_positioned": len(positioned_friendlies),
        "missing_friendlies": len(friendly_ids - positioned_friendlies),
    }


_CLASS_COLORS = {
    "deathknight": (196, 30, 59),
    "demonhunter": (163, 48, 201),
    "druid": (255, 124, 10),
    "evoker": (51, 147, 127),
    "hunter": (170, 211, 114),
    "mage": (63, 199, 235),
    "monk": (0, 255, 152),
    "paladin": (244, 140, 186),
    "priest": (245, 245, 245),
    "rogue": (255, 244, 104),
    "shaman": (0, 112, 221),
    "warlock": (135, 136, 238),
    "warrior": (198, 155, 109),
}
_DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


class _Canvas:
    def __init__(self, width, height, color):
        self.width, self.height = width, height
        self.pixels = bytearray(bytes(color) * width * height)

    def pixel(self, x, y, color):
        x, y = int(x), int(y)
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        offset = (y * self.width + x) * 3
        self.pixels[offset:offset + 3] = bytes(color)

    def rectangle(self, left, top, right, bottom, color):
        left, right = sorted((max(0, int(left)), min(self.width - 1, int(right))))
        top, bottom = sorted((max(0, int(top)), min(self.height - 1, int(bottom))))
        row = bytes(color) * max(0, right - left + 1)
        for y in range(top, bottom + 1):
            offset = (y * self.width + left) * 3
            self.pixels[offset:offset + len(row)] = row

    def line(self, x0, y0, x1, y1, color):
        x0, y0, x1, y1 = map(int, (x0, y0, x1, y1))
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self.pixel(x0, y0, color)
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

    def digits(self, value, center_x, center_y, color, scale=2):
        rendered = str(value)
        width = len(rendered) * 3 * scale + (len(rendered) - 1) * scale
        left = int(center_x - width / 2)
        top = int(center_y - (5 * scale) / 2)
        for digit in rendered:
            for row_index, row in enumerate(_DIGITS[digit]):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.rectangle(
                            left + column_index * scale,
                            top + row_index * scale,
                            left + (column_index + 1) * scale - 1,
                            top + (row_index + 1) * scale - 1,
                            color,
                        )
            left += 4 * scale


def _png_chunk(kind, data):
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", binascii.crc32(payload) & 0xFFFFFFFF)


def render_replay_png(snapshot, *, width=960, height=640):
    """Render a fixed-size marker map as a dependency-free RGB PNG."""
    if not (320 <= width <= 1600 and 240 <= height <= 1200):
        raise ReplayDataError("invalid_image_size")
    positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
    bounds = _valid_bounds(snapshot.get("bounds") if isinstance(snapshot, dict) else None)
    if not positions or not bounds:
        raise ReplayDataError("positions_unavailable")
    canvas = _Canvas(width, height, (13, 20, 32))
    left, top, right, bottom = 45, 30, width - 45, height - 30
    plot_width, plot_height = right - left, bottom - top
    span_x = bounds["maxX"] - bounds["minX"]
    span_y = bounds["maxY"] - bounds["minY"]
    scale = min(plot_width / span_x, plot_height / span_y)
    drawn_width, drawn_height = span_x * scale, span_y * scale
    origin_x = left + (plot_width - drawn_width) / 2
    origin_y = top + (plot_height - drawn_height) / 2
    grid = (36, 52, 73)
    border = (87, 108, 135)
    for index in range(11):
        x = origin_x + drawn_width * index / 10
        y = origin_y + drawn_height * index / 10
        canvas.line(x, origin_y, x, origin_y + drawn_height, grid)
        canvas.line(origin_x, y, origin_x + drawn_width, y, grid)
    canvas.line(origin_x, origin_y, origin_x + drawn_width, origin_y, border)
    canvas.line(origin_x, origin_y + drawn_height, origin_x + drawn_width,
                origin_y + drawn_height, border)
    canvas.line(origin_x, origin_y, origin_x, origin_y + drawn_height, border)
    canvas.line(origin_x + drawn_width, origin_y, origin_x + drawn_width,
                origin_y + drawn_height, border)

    def project(item):
        x = origin_x + (float(item["x"]) - bounds["minX"]) * scale
        y = origin_y + (bounds["maxY"] - float(item["y"])) * scale
        return (
            min(origin_x + drawn_width, max(origin_x, x)),
            min(origin_y + drawn_height, max(origin_y, y)),
        )

    for item in positions[:40]:
        x, y = project(item)
        if item.get("is_friendly"):
            key = re.sub(r"[^a-z]", "", str(item.get("sub_type") or "").casefold())
            color = _CLASS_COLORS.get(key, (103, 181, 255))
        else:
            color = (238, 68, 68) if item.get("is_boss") else (249, 115, 22)
        outline = (255, 255, 255) if item.get("is_boss") else (7, 11, 18)
        canvas.circle(x, y, 13, color, outline)
        hit_points, max_hit_points = item.get("hit_points"), item.get("max_hit_points")
        if (_finite_number(hit_points) and _finite_number(max_hit_points)
                and float(max_hit_points) > 0):
            ratio = min(1.0, max(0.0, float(hit_points) / float(max_hit_points)))
            canvas.rectangle(x - 12, y - 19, x + 12, y - 16, (44, 54, 68))
            if ratio > 0:
                canvas.rectangle(x - 12, y - 19, x - 12 + int(24 * ratio), y - 16,
                                 (52, 211, 153))
            else:
                canvas.line(x - 8, y - 8, x + 8, y + 8, (255, 255, 255))
                canvas.line(x + 8, y - 8, x - 8, y + 8, (255, 255, 255))
        canvas.digits(item.get("marker", 0), x, y, (8, 12, 20))

    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        start = row * stride
        raw.extend(canvas.pixels[start:start + stride])
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def _escape_markdown(value):
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~])", r"\\\1", str(value))


def replay_caption(snapshot, url):
    """Build a bounded English explanation and marker legend for Discord."""
    name = _escape_markdown(snapshot.get("fight_name") or "Unknown encounter")
    offset = format_fight_timestamp(snapshot.get("offset_ms") or 0)
    positioned = int(snapshot.get("friendly_positioned") or 0)
    total = int(snapshot.get("friendly_total") or 0)
    lines = [
        f"**Carter positional snapshot — {name}, fight {snapshot.get('fight_id')} at {offset}**",
        "Schematic API positions, not Warcraft Logs map artwork. Markers are the latest "
        "reported positions at or before that fight time.",
        f"Player coverage: **{positioned}/{total}**" if total else "Player coverage: unavailable",
    ]
    for item in snapshot.get("positions") or []:
        role = item.get("sub_type") if item.get("is_friendly") else (
            "boss" if item.get("is_boss") else "hostile"
        )
        health = ""
        if (_finite_number(item.get("hit_points")) and _finite_number(item.get("max_hit_points"))
                and float(item["max_hit_points"]) > 0):
            health = f" · {max(0, min(100, round(100 * item['hit_points'] / item['max_hit_points'])))}% HP"
        lines.append(
            f"`{item.get('marker')}` {_escape_markdown(item.get('name') or 'Unknown')}"
            f" — {_escape_markdown(role)}{health}"
        )
        if len("\n".join(lines)) > 1550:
            lines.append("…additional markers are shown in the image.")
            break
    lines.append(f"[Open the exact interactive Warcraft Logs replay]({url})")
    return "\n".join(lines)[:1900]
