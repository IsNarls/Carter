"""GUI-free World of Warcraft combat-log decoding primitives."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re


STAMP = re.compile(
    r'^(\d{1,2})/(\d{1,2})/(?:(\d{4}) )?(\d{1,2}):(\d{2}):(\d{2})'
    r'\.(\d+)([+-]\d{1,2}(?::\d{2})?)?\s{2,}(.*)$'
)


def decode_log_line(raw):
    """WoW lines can mix UTF-8 names with isolated Windows-1252 bytes."""
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        mixed = raw.decode('utf-8', errors='surrogateescape')
        text = ''.join(
            bytes((ord(character) - 0xDC00,)).decode('cp1252', errors='replace')
            if 0xDC80 <= ord(character) <= 0xDCFF else character
            for character in mixed
        )
        return text.removeprefix('\ufeff')


@dataclass
class Event:
    timestamp: float | None
    clock: str
    kind: str
    source: str
    target: str
    ability: str
    raw: str
    fields: tuple[str, ...] = ()


def parse_event(line, year=None):
    """Keep unknown records visible; decode common combat-event headers."""
    match = STAMP.match(line)
    if not match:
        return Event(None, '', 'UNPARSED', '', '', '', line)
    month, day, stamp_year, hour, minute, second, fraction, offset, payload = match.groups()
    timestamp = None
    try:
        offset_parts = (offset or '+0').split(':')
        minutes = int(offset_parts[0]) * 60
        if len(offset_parts) == 2:
            minutes += int(offset_parts[1]) * (-1 if offset.startswith('-') else 1)
        timestamp = datetime(
            int(stamp_year or year or datetime.now().year), int(month), int(day),
            int(hour), int(minute), int(second), int((fraction + '000000')[:6]),
            tzinfo=timezone(timedelta(minutes=minutes)),
        ).timestamp()
    except ValueError:
        pass
    try:
        fields = next(csv.reader([payload]))
    except (csv.Error, StopIteration):
        fields = [payload]
    kind = fields[0] if fields else 'UNKNOWN'
    source = target = ability = ''
    combat = kind.startswith(
        ('SPELL_', 'RANGE_', 'SWING_', 'DAMAGE_', 'ENVIRONMENTAL_', 'UNIT_'))
    if combat and len(fields) >= 9:
        source = '' if fields[2] == 'nil' else fields[2]
        target = '' if fields[6] == 'nil' else fields[6]
        if kind.startswith(
                ('SPELL_', 'RANGE_', 'DAMAGE_SHIELD', 'DAMAGE_SPLIT')) and len(fields) > 10:
            ability = fields[10]
        elif kind.startswith('SWING_'):
            ability = 'Melee'
        elif kind == 'ENVIRONMENTAL_DAMAGE' and len(fields) > 9:
            ability = fields[9]
    elif kind in ('ENCOUNTER_START', 'ENCOUNTER_END', 'ZONE_CHANGE', 'MAP_CHANGE') \
            and len(fields) > 2:
        ability = fields[2]
    return Event(timestamp, f'{hour}:{minute}:{second}.{fraction}', kind,
                 source, target, ability, line, tuple(fields))
