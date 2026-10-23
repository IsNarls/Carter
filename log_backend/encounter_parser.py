"""Build boss-health and damage timelines from WoW advanced combat logs."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

if __package__:
    from .combat_log_core import Event, decode_log_line, parse_event
else:  # Support direct execution from this directory.
    from combat_log_core import Event, decode_log_line, parse_event


DAMAGE_EVENTS = {
    'SWING_DAMAGE': 9,
    'SWING_DAMAGE_LANDED': 9,
    'RANGE_DAMAGE': 12,
    'SPELL_DAMAGE': 12,
    'SPELL_PERIODIC_DAMAGE': 12,
    'DAMAGE_SHIELD': 12,
    'DAMAGE_SPLIT': 12,
    'ENVIRONMENTAL_DAMAGE': 10,
}
HEAL_EVENTS = {'SPELL_HEAL', 'SPELL_PERIODIC_HEAL'}
SPEC_CLASSES = {
    250: 'Death Knight', 251: 'Death Knight', 252: 'Death Knight',
    577: 'Demon Hunter', 581: 'Demon Hunter', 1480: 'Demon Hunter',
    102: 'Druid', 103: 'Druid', 104: 'Druid', 105: 'Druid',
    1467: 'Evoker', 1468: 'Evoker', 1473: 'Evoker',
    253: 'Hunter', 254: 'Hunter', 255: 'Hunter',
    62: 'Mage', 63: 'Mage', 64: 'Mage',
    268: 'Monk', 269: 'Monk', 270: 'Monk',
    65: 'Paladin', 66: 'Paladin', 70: 'Paladin',
    256: 'Priest', 257: 'Priest', 258: 'Priest',
    259: 'Rogue', 260: 'Rogue', 261: 'Rogue',
    262: 'Shaman', 263: 'Shaman', 264: 'Shaman',
    265: 'Warlock', 266: 'Warlock', 267: 'Warlock',
    71: 'Warrior', 72: 'Warrior', 73: 'Warrior',
}
WOW_CLASS_COLORS = {
    'Death Knight': '#C41E3A', 'Demon Hunter': '#A330C9', 'Druid': '#FF7C0A',
    'Evoker': '#33937F', 'Hunter': '#AAD372', 'Mage': '#3FC7EB',
    'Monk': '#00FF98', 'Paladin': '#F48CBA', 'Priest': '#FFFFFF',
    'Rogue': '#FFF468', 'Shaman': '#0070DD', 'Warlock': '#8788EE',
    'Warrior': '#C69B6D', 'Unknown': '#35A7FF',
}


def number(value: str, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


@dataclass
class Damage:
    amount: int
    effective: int
    overkill: int
    current_health: int
    max_health: int
    snapshot_guid: str


@dataclass
class Snapshot:
    guid: str
    owner_guid: str
    x: float
    y: float
    map_id: int


def unit_snapshot(event: Event) -> Snapshot | None:
    """Return the advanced unit snapshot attached to a common combat event."""
    if event.kind.startswith('SWING_'):
        start = 9
    elif event.kind.startswith(('SPELL_', 'RANGE_')):
        start = 12
    elif event.kind.startswith('ENVIRONMENTAL_'):
        start = 10
    else:
        return None
    fields = event.fields
    if len(fields) <= start + 16:
        return None
    guid = fields[start]
    if not guid.startswith(('Player-', 'Pet-', 'Creature-', 'Vehicle-')):
        return None
    try:
        x = float(fields[start + 14])
        y = float(fields[start + 15])
        map_id = int(fields[start + 16])
    except (TypeError, ValueError):
        return None
    owner = fields[start + 1]
    return Snapshot(guid, '' if owner in ('nil', '0000000000000000') else owner,
                    x, y, map_id)


def damage_from_event(event: Event) -> Damage | None:
    """Decode an advanced-log damage record.

    The event-specific prefix is followed by the 19-field advanced unit snapshot,
    then amount, raw amount, and overkill. Logs without that snapshot return None because they
    cannot provide a trustworthy health timeline.
    """
    snapshot = DAMAGE_EVENTS.get(event.kind)
    fields = event.fields
    if snapshot is None or len(fields) <= snapshot + 21:
        return None
    unit_guid = fields[snapshot]
    if not unit_guid or unit_guid in ('nil', '0000000000000000'):
        return None
    current = number(fields[snapshot + 2], -1)
    maximum = number(fields[snapshot + 3], -1)
    amount = number(fields[snapshot + 19], -1)
    # Combat log v22 inserts raw/unmitigated amount before overkill.
    overkill = number(fields[snapshot + 21], 0)
    if min(current, maximum, amount) < 0 or maximum == 0:
        return None
    effective = max(0, amount - max(0, overkill))
    return Damage(amount, effective, overkill, current, maximum, unit_guid)


def healing_from_event(event: Event) -> int | None:
    """Return effective healing from a v22 heal record."""
    if event.kind not in HEAL_EVENTS or len(event.fields) <= 31:
        return None
    # v22 reports effective healing first, followed by the pre-overheal amount
    # and other heal details. A completely overhealed event therefore has zero.
    amount = number(event.fields[31], -1)
    return max(0, amount) if amount >= 0 else None


def absorb_from_event(event: Event) -> tuple[str, str, int] | None:
    """Return shield owner GUID/name and effective absorbed damage."""
    if event.kind != 'SPELL_ABSORBED':
        return None
    fields = event.fields
    # Swing absorbs put the shield owner at field 9. Spell absorbs include the
    # damaging spell first and put the shield owner at field 12.
    owner_index = 9 if len(fields) > 9 and fields[9].startswith(
        ('Player-', 'Pet-', 'Creature-', 'Vehicle-')) else 12
    amount_index = owner_index + 7
    if len(fields) <= amount_index:
        return None
    guid = fields[owner_index]
    amount = number(fields[amount_index], -1)
    if not guid.startswith(('Player-', 'Pet-', 'Creature-', 'Vehicle-')) or amount < 0:
        return None
    name = fields[owner_index + 1]
    return guid, name, amount


@dataclass
class EncounterRef:
    attempt: int
    encounter_id: int
    name: str
    difficulty: int
    group_size: int
    start: float
    end: float | None = None
    success: bool = False
    start_offset: int = 0
    end_offset: int | None = None
    external_id: int | None = None

    @property
    def duration(self):
        return (self.end - self.start) if self.end is not None else None


def scan_encounters(path: Path) -> list[EncounterRef]:
    """Find pull boundaries without parsing millions of ordinary combat lines."""
    if str(path).lower().endswith('.wcl.json'):
        return scan_warcraft_logs_encounters(path)
    encounters = []
    active = None
    with path.open('rb') as log:
        while True:
            offset = log.tell()
            raw = log.readline()
            if not raw:
                break
            if b'ENCOUNTER_START,' not in raw and b'ENCOUNTER_END,' not in raw:
                continue
            event = parse_event(decode_log_line(raw).rstrip('\r\n'))
            fields = event.fields
            if event.kind == 'ENCOUNTER_START' and len(fields) >= 5 and event.timestamp is not None:
                active = EncounterRef(
                    len(encounters) + 1, number(fields[1]), fields[2], number(fields[3]),
                    number(fields[4]), event.timestamp, start_offset=offset,
                )
                encounters.append(active)
            elif event.kind == 'ENCOUNTER_END' and active and len(fields) >= 6:
                if number(fields[1]) == active.encounter_id:
                    active.end = event.timestamp
                    active.success = fields[5] == '1'
                    active.end_offset = log.tell()
                    active = None
    return encounters


@dataclass
class Unit:
    guid: str
    name: str
    max_health: int = 0
    damage: int = 0
    observations: int = 0


@dataclass
class BossProfile:
    guid: str
    name: str
    positions: dict[int, Position] = field(default_factory=dict)


@dataclass
class MechanicEvent:
    start: float
    end: float
    boss_guid: str
    boss_name: str
    spell_id: int
    ability: str
    occurrence: int
    since_previous: float | None = None
    completed: bool = True


@dataclass
class Player:
    guid: str
    name: str
    damage: int
    dps: float
    class_name: str = 'Unknown'
    rank: int = 0
    healing: int = 0
    hps: float = 0.0
    healing_rank: int = 0
    position_samples: int = 0


@dataclass
class Position:
    x: float
    y: float
    map_id: int
    age: int = 0


@dataclass
class TimelineRow:
    second: int
    health: int | None
    max_health: int | None
    damage: int
    events: list[str] = field(default_factory=list)
    unit_health: dict[str, tuple[int, int]] = field(default_factory=dict)
    unit_damage: dict[str, int] = field(default_factory=dict)
    player_damage: dict[str, int] = field(default_factory=dict)
    player_healing: dict[str, int] = field(default_factory=dict)
    player_positions: dict[str, Position] = field(default_factory=dict)
    boss_positions: dict[str, Position] = field(default_factory=dict)

    @property
    def health_pct(self):
        if self.health is None or not self.max_health:
            return None
        return 100 * self.health / self.max_health


@dataclass
class EncounterTimeline:
    encounter: EncounterRef
    bosses: list[Unit]
    rows: list[TimelineRow]
    players: list[Player] = field(default_factory=list)
    boss_profiles: list[BossProfile] = field(default_factory=list)
    mechanics: list[MechanicEvent] = field(default_factory=list)
    deaths: list[DeathRecord] = field(default_factory=list)
    position_history: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class DeathRecord:
    death_id: str
    player_guid: str
    player_name: str
    class_name: str
    death_number: int
    time: float
    killing_blow: dict | None = None
    primary_cause: dict | None = None
    damage_before_death: list[dict] = field(default_factory=list)
    healing_before_death: list[dict] = field(default_factory=list)
    position: dict | None = None
    spatial_context: dict = field(default_factory=dict)
    active_mechanics: list[dict] = field(default_factory=list)
    debuffs: list[dict] = field(default_factory=list)


def scan_warcraft_logs_encounters(path: Path) -> list[EncounterRef]:
    """Expose imported Warcraft Logs fights through the local encounter interface."""
    if __package__:
        from .warcraft_logs_importer import load_cache
    else:
        from warcraft_logs_importer import load_cache

    report = load_cache(path)['report']
    encounters = []
    for fight in report.get('fights', []):
        if not fight.get('encounterID'):
            continue
        start_ms = float(fight.get('startTime') or 0)
        end_ms = float(fight.get('endTime') or start_ms)
        encounters.append(EncounterRef(
            attempt=len(encounters) + 1,
            encounter_id=number(fight.get('encounterID')),
            name=str(fight.get('name') or 'Encounter'),
            difficulty=number(fight.get('difficulty')),
            group_size=number(fight.get('size')),
            start=start_ms / 1000.0,
            end=end_ms / 1000.0,
            success=bool(fight.get('kill')),
            external_id=number(fight.get('id')),
        ))
    return encounters


def _wcl_position(resources, fallback=None) -> Position | None:
    resources = resources if isinstance(resources, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    x = resources.get('x', fallback.get('x'))
    y = resources.get('y', fallback.get('y'))
    if x is None or y is None:
        return None
    try:
        return Position(float(x), float(y), number(
            resources.get('mapID', resources.get('mapId', fallback.get('mapID', 0)))))
    except (TypeError, ValueError):
        return None


def _wcl_resource_health(resources, fallback=None) -> tuple[int, int] | None:
    resources = resources if isinstance(resources, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    current = resources.get('hitPoints', fallback.get('hitPoints'))
    maximum = resources.get('maxHitPoints', fallback.get('maxHitPoints'))
    current, maximum = number(current, -1), number(maximum, -1)
    return (current, maximum) if current >= 0 and maximum > 0 else None


def analyze_warcraft_logs_encounter(path: Path, encounter: EncounterRef) -> EncounterTimeline:
    """Normalize Warcraft Logs API events into the application's timeline model."""
    if __package__:
        from .warcraft_logs_importer import load_cache
    else:
        from warcraft_logs_importer import load_cache

    payload = load_cache(path)
    report = payload['report']
    master = report.get('masterData') or {}
    actors = {number(actor.get('id')): actor for actor in master.get('actors', [])}
    abilities = {number(ability.get('gameID')): str(ability.get('name') or '')
                 for ability in master.get('abilities', [])}
    fight_id = encounter.external_id
    if fight_id is None:
        refs = scan_warcraft_logs_encounters(path)
        match = next((ref for ref in refs if ref.attempt == encounter.attempt), None)
        fight_id = match.external_id if match else None
    events = payload.get('eventsByFight', {}).get(str(fight_id), [])
    if fight_id is None or not isinstance(events, list):
        raise ValueError('The selected fight is missing from the Warcraft Logs cache.')

    def actor_guid(actor_id, instance=None):
        base = f'WCL-{number(actor_id)}'
        actor = actors.get(number(actor_id)) or {}
        if str(actor.get('type') or '') in ('NPC', 'Pet') and number(instance) > 0:
            return f'{base}:{number(instance)}'
        return base

    def base_actor_guid(guid):
        return str(guid).split(':', 1)[0]

    actor_name = lambda actor_id: str((actors.get(number(actor_id)) or {}).get('name') or
                                      actor_guid(actor_id))
    actor_type = lambda actor_id: str((actors.get(number(actor_id)) or {}).get('type') or '')
    names = {actor_guid(actor_id): actor_name(actor_id) for actor_id in actors}
    pet_owners = {
        actor_guid(actor_id): actor_guid(actor.get('petOwner'))
        for actor_id, actor in actors.items() if actor.get('petOwner')
    }
    class_names = {
        actor_guid(actor_id): str(actor.get('subType') or 'Unknown').replace('DeathKnight', 'Death Knight')
        for actor_id, actor in actors.items() if str(actor.get('type')) == 'Player'
    }
    units: dict[str, Unit] = {}
    health: dict[tuple[int, str], tuple[int, int]] = {}
    damage: dict[tuple[int, str], int] = {}
    attacks: list[tuple[int, str, str, int]] = []
    heals: list[tuple[int, str, int]] = []
    positions: dict[tuple[int, str], Position] = {}
    exact_positions: dict[str, list[tuple[float, Position]]] = {}
    creature_casts: list[tuple[float, str, str, str, int]] = []
    player_deaths: list[tuple[int, str]] = []
    death_events: list[tuple[float, dict]] = []
    incoming_damage: dict[str, list[dict]] = {}
    incoming_healing: dict[str, list[dict]] = {}
    active_debuffs: dict[str, dict[tuple[int, str], dict]] = {}
    debuffs_at_death: dict[int, list[dict]] = {}
    fight_start_ms = encounter.start * 1000.0

    for event in events:
        timestamp = float(event.get('timestamp') or fight_start_ms)
        elapsed = max(0.0, (timestamp - fight_start_ms) / 1000.0)
        second = int(elapsed)
        kind = str(event.get('type') or '').lower()
        source_id, target_id = event.get('sourceID'), event.get('targetID')
        source_guid = actor_guid(source_id, event.get('sourceInstance')) if source_id is not None else ''
        target_guid = actor_guid(target_id, event.get('targetInstance')) if target_id is not None else ''
        if source_guid:
            names[source_guid] = actor_name(source_id)
        if target_guid:
            names[target_guid] = actor_name(target_id)
        source_resources = event.get('sourceResources')
        target_resources = event.get('targetResources')

        source_position = _wcl_position(source_resources)
        target_position = _wcl_position(target_resources)
        if source_guid and source_position:
            positions[second, source_guid] = source_position
            exact_positions.setdefault(source_guid, []).append((elapsed, source_position))
        if target_guid and target_position:
            positions[second, target_guid] = target_position
            exact_positions.setdefault(target_guid, []).append((elapsed, target_position))
        direct = _wcl_position(event)
        resource_actor = number(event.get('resourceActor'), 0)
        if direct:
            if resource_actor == 1 and source_guid and not source_position:
                positions[second, source_guid] = direct
                exact_positions.setdefault(source_guid, []).append((elapsed, direct))
            elif resource_actor == 2 and target_guid and not target_position:
                positions[second, target_guid] = direct
                exact_positions.setdefault(target_guid, []).append((elapsed, direct))
            elif not resource_actor and target_guid and kind in ('damage', 'heal', 'absorbed'):
                positions[second, target_guid] = direct
                exact_positions.setdefault(target_guid, []).append((elapsed, direct))
            elif not resource_actor and source_guid and kind in ('cast', 'begincast'):
                positions[second, source_guid] = direct
                exact_positions.setdefault(source_guid, []).append((elapsed, direct))

        target_observation = _wcl_resource_health(target_resources)
        if target_observation is None and resource_actor == 2:
            target_observation = _wcl_resource_health(event)

        if kind == 'damage' and target_guid and actor_type(target_id) in ('NPC', 'Pet'):
            amount = max(0, number(event.get('amount')) - max(0, number(event.get('overkill'))))
            unit = units.setdefault(target_guid, Unit(target_guid, actor_name(target_id)))
            unit.damage += amount
            damage[second, target_guid] = damage.get((second, target_guid), 0) + amount
            attacks.append((second, target_guid, source_guid, amount))
            observation = target_observation
            if observation:
                unit.max_health = max(unit.max_health, observation[1])
                unit.observations += 1
                health[second, target_guid] = observation
        if kind == 'heal' and source_guid:
            # WCL amount is effective healing. absorbedHealing represents shield
            # healing attributed to the same source and is additive when present.
            amount = max(0, number(event.get('amount'))) + max(
                0, number(event.get('absorbedHealing')))
            heals.append((second, source_guid, amount))
            if target_guid and actor_type(target_id) == 'Player':
                incoming_healing.setdefault(target_guid, []).append({
                    'time': elapsed, 'source_guid': source_guid,
                    'source': actor_name(source_id),
                    'ability_id': number(event.get('abilityGameID', event.get('abilityID'))),
                    'ability': abilities.get(number(event.get('abilityGameID', event.get('abilityID'))),
                                             f'Spell {number(event.get("abilityGameID", event.get("abilityID")))}'),
                    'amount': amount,
                    'absorbed': max(0, number(event.get('absorbedHealing'))),
                })
        elif kind == 'absorbed' and source_guid:
            heals.append((second, source_guid, max(0, number(event.get('amount')))))
        if kind in ('begincast', 'cast') and source_guid and actor_type(source_id) in ('NPC', 'Pet'):
            spell_id = number(event.get('abilityGameID', event.get('abilityID')))
            ability = abilities.get(spell_id) or str(event.get('ability') or f'Spell {spell_id}')
            if str((actors.get(number(source_id)) or {}).get('subType') or '').lower() == 'boss':
                units.setdefault(source_guid, Unit(source_guid, actor_name(source_id)))
            creature_casts.append((elapsed, source_guid,
                                   'SPELL_CAST_START' if kind == 'begincast' else 'SPELL_CAST_SUCCESS',
                                   ability, spell_id))
        spell_id = number(event.get('abilityGameID', event.get('abilityID')))
        debuff_key = (spell_id, source_guid)
        if target_guid and actor_type(target_id) == 'Player':
            if kind in ('applydebuff', 'applydebuffstack', 'refreshdebuff'):
                active_debuffs.setdefault(target_guid, {})[debuff_key] = {
                    'ability_id': spell_id,
                    'ability': abilities.get(spell_id, f'Spell {spell_id}'),
                    'source_guid': source_guid, 'source': actor_name(source_id),
                    'applied_at': elapsed, 'stacks': number(event.get('stack'), 1),
                }
            elif kind == 'removedebuff':
                active_debuffs.setdefault(target_guid, {}).pop(debuff_key, None)
        if kind == 'death' and target_guid and actor_type(target_id) == 'Player':
            player_deaths.append((second, actor_name(target_id)))
            death_events.append((elapsed, event))
            debuffs_at_death[id(event)] = list(active_debuffs.get(target_guid, {}).values())
        if kind == 'damage' and target_guid and actor_type(target_id) == 'Player':
            spell_id = number(event.get('abilityGameID', event.get('abilityID')))
            raw_amount = max(0, number(event.get('amount')))
            overkill = max(0, number(event.get('overkill')))
            health_after = None if target_observation is None else target_observation[0]
            effective = max(0, raw_amount - overkill)
            incoming_damage.setdefault(target_guid, []).append({
                'time': elapsed, 'source_guid': source_guid, 'source': actor_name(source_id),
                'ability_id': spell_id, 'ability': abilities.get(spell_id, f'Spell {spell_id}'),
                'amount': raw_amount, 'effective': effective,
                'overkill': overkill,
                'health_before': None if health_after is None else health_after + effective,
                'health_after': health_after,
                'max_health': None if target_observation is None else target_observation[1],
            })

    largest = max((unit.max_health for unit in units.values()), default=0)
    bosses = [unit for unit in units.values()
              if str((actors.get(number(base_actor_guid(unit.guid).removeprefix('WCL-'))) or {})
                     .get('subType') or '').lower() == 'boss']
    if not bosses:
        bosses = [unit for unit in units.values()
                  if unit.observations and unit.max_health >= largest * 0.15]
    if not bosses and units:
        bosses = [max(units.values(), key=lambda unit: (unit.max_health, unit.damage))]
    bosses.sort(key=lambda unit: (-unit.max_health, unit.name))
    boss_guids = {boss.guid for boss in bosses}
    boss_profiles = [BossProfile(
        boss.guid, boss.name,
        {second: position for (second, guid), position in positions.items() if guid == boss.guid},
    ) for boss in bosses]

    mechanics: list[MechanicEvent] = []
    pending: dict[tuple[str, int], list[tuple[float, str]]] = {}
    occurrences: dict[tuple[str, int], int] = {}
    previous: dict[tuple[str, int], float] = {}

    def add_mechanic(start, end, guid, spell_id, ability, completed=True):
        key = (guid, spell_id)
        occurrences[key] = occurrences.get(key, 0) + 1
        prior = previous.get(key)
        mechanics.append(MechanicEvent(
            start, end, guid, names.get(guid, guid), spell_id, ability,
            occurrences[key], None if prior is None else start - prior, completed))
        previous[key] = start

    for cast_time, guid, kind, ability, spell_id in creature_casts:
        if guid not in boss_guids:
            continue
        key = (guid, spell_id)
        if kind == 'SPELL_CAST_START':
            pending.setdefault(key, []).append((cast_time, ability))
        elif pending.get(key):
            start, started_ability = pending[key].pop(0)
            add_mechanic(start, cast_time, guid, spell_id, started_ability or ability)
        else:
            add_mechanic(cast_time, cast_time, guid, spell_id, ability)
    for (guid, spell_id), casts in pending.items():
        for start, ability in casts:
            add_mechanic(start, start + 3.0, guid, spell_id, ability, False)
    mechanics.sort(key=lambda item: item.start)

    player_damage: dict[tuple[int, str], int] = {}
    player_healing: dict[tuple[int, str], int] = {}
    totals: dict[str, int] = {}
    healing_totals: dict[str, int] = {}
    for second, target_guid, source_guid, amount in attacks:
        if target_guid not in boss_guids:
            continue
        owner = source_guid if source_guid in class_names else pet_owners.get(
            base_actor_guid(source_guid))
        if owner:
            player_damage[second, owner] = player_damage.get((second, owner), 0) + amount
            totals[owner] = totals.get(owner, 0) + amount
    for second, source_guid, amount in heals:
        owner = source_guid if source_guid in class_names else pet_owners.get(
            base_actor_guid(source_guid))
        if owner:
            player_healing[second, owner] = player_healing.get((second, owner), 0) + amount
            healing_totals[owner] = healing_totals.get(owner, 0) + amount
    duration_seconds = max(encounter.duration or 0, 0.001)
    death_player_guids = {actor_guid(event.get('targetID')) for _time, event in death_events}
    players = [Player(
        guid, names.get(guid, guid), totals.get(guid, 0), totals.get(guid, 0) / duration_seconds,
        class_name=class_names.get(guid, 'Unknown'),
        healing=healing_totals.get(guid, 0), hps=healing_totals.get(guid, 0) / duration_seconds,
        position_samples=sum(1 for _second, seen_guid in positions if seen_guid == guid),
    ) for guid in totals.keys() | healing_totals.keys() | death_player_guids]
    players.sort(key=lambda player: (-player.damage, player.name))
    for rank, player in enumerate(players, 1):
        player.rank = rank
    for rank, player in enumerate(sorted((p for p in players if p.healing),
                                         key=lambda p: (-p.healing, p.name)), 1):
        player.healing_rank = rank
    player_guids = {player.guid for player in players}

    def position_before(guid: str, moment: float):
        observations = exact_positions.get(guid, [])
        return next(((seen, pos) for seen, pos in reversed(observations) if seen <= moment), None)

    death_counts: dict[str, int] = {}
    deaths: list[DeathRecord] = []
    for death_time, death_event in sorted(death_events, key=lambda item: item[0]):
        target_id = death_event.get('targetID')
        player_guid = actor_guid(target_id, death_event.get('targetInstance'))
        death_counts[player_guid] = death_counts.get(player_guid, 0) + 1
        history = [item for item in incoming_damage.get(player_guid, [])
                   if death_time - 30.0 <= item['time'] <= death_time]
        healing_history = [item for item in incoming_healing.get(player_guid, [])
                           if death_time - 30.0 <= item['time'] <= death_time]
        killer_id = death_event.get('killerID')
        killer_guid = (actor_guid(killer_id, death_event.get('killerInstanceID'))
                       if killer_id is not None else None)
        killing_spell = number(death_event.get('killingAbilityGameID'))
        candidates = [item for item in history if death_time - item['time'] <= 2.0]
        matching = [item for item in candidates
                    if (not killing_spell or item['ability_id'] == killing_spell)
                    and (killer_id is None or item['source_guid'] == killer_guid)]
        lethal = (matching or candidates)[-1] if (matching or candidates) else None
        killing_blow = {
            'source_guid': killer_guid if killer_id is not None else (
                lethal.get('source_guid') if lethal else None),
            'source': actor_name(killer_id) if killer_id is not None else (
                lethal.get('source') if lethal else None),
            'ability_id': killing_spell or (lethal.get('ability_id') if lethal else 0),
            'ability': abilities.get(killing_spell) if killing_spell else (
                lethal.get('ability') if lethal else None),
            'amount': lethal.get('amount') if lethal else None,
            'overkill': lethal.get('overkill') if lethal else None,
            'provenance': 'reported' if killer_id is not None or killing_spell else 'inferred',
            'confidence': 'high' if killer_id is not None and killing_spell else (
                'moderate' if lethal else 'low'),
        }
        recent = [item for item in history if death_time - item['time'] <= 5.0]
        primary_event = max(recent, key=lambda item: item['amount'], default=lethal)
        primary_cause = None if primary_event is None else {
            'type': 'damage_event', 'source': primary_event['source'],
            'ability_id': primary_event['ability_id'], 'ability': primary_event['ability'],
            'amount': primary_event['amount'], 'provenance': 'inferred',
            'confidence': 'moderate',
            'reason': ('Largest damage event in the five seconds before death.'
                       if primary_event is not lethal else
                       'The lethal event was also the strongest recent damage event.'),
        }
        contributing_events = [item for item in recent if item is not primary_event]
        if primary_cause is not None:
            primary_cause['contributing_events'] = contributing_events
        seen_position = position_before(player_guid, death_time)
        position_data = None
        spatial = {'boss_distances': [], 'distance_to_raid_center': None,
                   'nearby_players': [], 'players_within_5_yards': 0}
        if seen_position:
            seen_at, pos = seen_position
            age = max(0.0, death_time - seen_at)
            confidence = 'high' if age < 0.5 else ('moderate' if age <= 2.0 else 'stale')
            position_data = {'x': pos.x, 'y': pos.y, 'map_id': pos.map_id,
                             'observed_at': seen_at, 'sample_age_seconds': age,
                             'confidence': confidence}
            peers = []
            for guid in class_names:
                if guid == player_guid:
                    continue
                observed = position_before(guid, death_time)
                if not observed or death_time - observed[0] > 2.0 or observed[1].map_id != pos.map_id:
                    continue
                distance = math.hypot(observed[1].x - pos.x, observed[1].y - pos.y)
                peers.append((guid, observed[1], distance))
            if peers:
                center_x = sum(item[1].x for item in peers) / len(peers)
                center_y = sum(item[1].y for item in peers) / len(peers)
                spatial['distance_to_raid_center'] = math.hypot(center_x - pos.x, center_y - pos.y)
                spatial['nearby_players'] = [
                    {'player_guid': guid, 'name': names.get(guid, guid), 'distance': distance}
                    for guid, _peer_pos, distance in sorted(peers, key=lambda item: item[2])[:10]
                    if distance <= 10.0
                ]
                spatial['players_within_5_yards'] = sum(1 for _g, _p, distance in peers
                                                        if distance <= 5.0)
            for boss_guid in boss_guids:
                observed = position_before(boss_guid, death_time)
                if observed and observed[1].map_id == pos.map_id:
                    spatial['boss_distances'].append({
                        'boss_guid': boss_guid, 'boss': names.get(boss_guid, boss_guid),
                        'distance': math.hypot(observed[1].x - pos.x, observed[1].y - pos.y),
                        'sample_age_seconds': max(0.0, death_time - observed[0]),
                    })
        active_mechanics = [asdict(mechanic) for mechanic in mechanics
                            if mechanic.start <= death_time <= max(mechanic.end, mechanic.start)]
        death_id = f'fight-{fight_id}:{player_guid}:{round(death_time * 1000)}'
        deaths.append(DeathRecord(
            death_id, player_guid, actor_name(target_id), class_names.get(player_guid, 'Unknown'),
            death_counts[player_guid], death_time, killing_blow, primary_cause,
            history, healing_history, position_data, spatial, active_mechanics,
            debuffs_at_death.get(id(death_event), []),
        ))

    final_second = max(0, int(encounter.duration or 0))
    notes: dict[int, list[str]] = {0: [f'Pull: {encounter.name}']}
    for cast_time, guid, kind, ability, _spell_id in creature_casts:
        if guid in boss_guids:
            notes.setdefault(int(cast_time), []).append(
                f'{names.get(guid, guid)} {"begins" if kind == "SPELL_CAST_START" else "casts"} {ability}')
    for second, name in player_deaths:
        notes.setdefault(second, []).append(f'{name} died')
    notes.setdefault(final_second, []).append('Kill' if encounter.success else 'Wipe')

    latest_health: dict[str, tuple[int, int]] = {}
    last_seen: dict[str, int] = {}
    latest_player_positions: dict[str, tuple[int, Position]] = {}
    latest_boss_positions: dict[str, tuple[int, Position]] = {}
    rows = []
    for second in range(final_second + 1):
        for guid in boss_guids:
            if (second, guid) in health:
                latest_health[guid] = health[second, guid]
                last_seen[guid] = second
        active = {guid: latest_health[guid] for guid in boss_guids
                  if guid in latest_health and second - last_seen[guid] <= 3}
        for guid in player_guids:
            if (second, guid) in positions:
                latest_player_positions[guid] = (second, positions[second, guid])
        for guid in boss_guids:
            if (second, guid) in positions:
                latest_boss_positions[guid] = (second, positions[second, guid])
        rows.append(TimelineRow(
            second,
            sum(item[0] for item in active.values()) if active else None,
            sum(item[1] for item in active.values()) if active else None,
            sum(damage.get((second, guid), 0) for guid in boss_guids),
            notes.get(second, []), active,
            {guid: damage.get((second, guid), 0) for guid in boss_guids},
            {guid: player_damage.get((second, guid), 0) for guid in player_guids},
            {guid: player_healing.get((second, guid), 0) for guid in player_guids},
            {guid: Position(pos.x, pos.y, pos.map_id, second - seen)
             for guid, (seen, pos) in latest_player_positions.items()},
            {guid: Position(pos.x, pos.y, pos.map_id, second - seen)
             for guid, (seen, pos) in latest_boss_positions.items() if guid in active},
        ))
    position_history = {
        guid: [{'time': seen, 'x': position.x, 'y': position.y, 'map_id': position.map_id}
               for seen, position in observations]
        for guid, observations in exact_positions.items()
    }
    return EncounterTimeline(encounter, bosses, rows, players, boss_profiles, mechanics, deaths,
                             position_history)


ANALYSIS_CACHE_VERSION = 4


def _analysis_cache_path(path: Path, encounter: EncounterRef) -> Path:
    if __package__:
        from .warcraft_logs_importer import analysis_directory
    else:
        from warcraft_logs_importer import analysis_directory

    fight_key = encounter.external_id if encounter.external_id is not None else encounter.attempt
    return analysis_directory(path) / f'fight-{fight_key}.json.gz'


def _source_signature(path: Path) -> dict[str, int]:
    stat = Path(path).stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def _timeline_from_dict(data: dict) -> EncounterTimeline:
    encounter = EncounterRef(**data['encounter'])
    bosses = [Unit(**item) for item in data.get('bosses', [])]
    players = [Player(**item) for item in data.get('players', [])]
    mechanics = [MechanicEvent(**item) for item in data.get('mechanics', [])]
    deaths = [DeathRecord(**item) for item in data.get('deaths', [])]
    profiles = [BossProfile(
        item['guid'], item['name'],
        {int(second): Position(**position)
         for second, position in item.get('positions', {}).items()},
    ) for item in data.get('boss_profiles', [])]
    rows = []
    for item in data.get('rows', []):
        rows.append(TimelineRow(
            second=item['second'], health=item.get('health'), max_health=item.get('max_health'),
            damage=item.get('damage', 0), events=item.get('events', []),
            unit_health={guid: tuple(values)
                         for guid, values in item.get('unit_health', {}).items()},
            unit_damage=item.get('unit_damage', {}),
            player_damage=item.get('player_damage', {}),
            player_healing=item.get('player_healing', {}),
            player_positions={guid: Position(**position)
                              for guid, position in item.get('player_positions', {}).items()},
            boss_positions={guid: Position(**position)
                            for guid, position in item.get('boss_positions', {}).items()},
        ))
    return EncounterTimeline(encounter, bosses, rows, players, profiles, mechanics, deaths,
                             data.get('position_history', {}))


def load_saved_analysis(path: Path, encounter: EncounterRef) -> EncounterTimeline | None:
    """Load a persisted fight analysis when it matches the downloaded report."""
    cache_path = _analysis_cache_path(path, encounter)
    if not cache_path.is_file():
        return None
    try:
        with gzip.open(cache_path, 'rt', encoding='utf-8') as source:
            payload = json.load(source)
        if payload.get('version') != ANALYSIS_CACHE_VERSION:
            return None
        if payload.get('source') != _source_signature(path):
            return None
        return _timeline_from_dict(payload['timeline'])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_analysis(path: Path, timeline: EncounterTimeline) -> Path:
    """Persist a completed Warcraft Logs analysis in a compressed sidecar."""
    cache_path = _analysis_cache_path(path, timeline.encounter)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + '.part')
    payload = {
        'version': ANALYSIS_CACHE_VERSION,
        'source': _source_signature(path),
        'timeline': asdict(timeline),
    }
    with gzip.open(temporary, 'wt', encoding='utf-8') as output:
        json.dump(payload, output, ensure_ascii=False, separators=(',', ':'))
    temporary.replace(cache_path)
    return cache_path


def encounter_events(path: Path, encounter: EncounterRef) -> Iterator[Event]:
    with path.open('rb') as log:
        log.seek(encounter.start_offset)
        while encounter.end_offset is None or log.tell() < encounter.end_offset:
            raw = log.readline()
            if not raw:
                return
            yield parse_event(decode_log_line(raw).rstrip('\r\n'))


def analyze_encounter(path: Path, encounter: EncounterRef) -> EncounterTimeline:
    if str(path).lower().endswith('.wcl.json'):
        cached = load_saved_analysis(path, encounter)
        if cached is not None:
            return cached
        timeline = analyze_warcraft_logs_encounter(path, encounter)
        save_analysis(path, timeline)
        return timeline
    units: dict[str, Unit] = {}
    health: dict[tuple[int, str], tuple[int, int]] = {}
    damage: dict[tuple[int, str], int] = {}
    creature_casts: list[tuple[float, str, str, str, int]] = []
    player_deaths: list[tuple[int, str]] = []
    attacks: list[tuple[int, str, str, int]] = []
    heals: list[tuple[int, str, int]] = []
    names: dict[str, str] = {}
    pet_owners: dict[str, str] = {}
    unit_positions: dict[tuple[int, str], Position] = {}
    player_specs: dict[str, int] = {}
    exact_positions: dict[str, list[tuple[float, Position]]] = {}
    death_events: list[tuple[float, str, str]] = []
    incoming_damage: dict[str, list[dict]] = {}
    incoming_healing: dict[str, list[dict]] = {}
    active_debuffs: dict[str, dict[tuple[int, str], dict]] = {}
    debuffs_at_death: dict[tuple[str, int], list[dict]] = {}
    last_elapsed = 0.0

    for event in encounter_events(path, encounter):
        if event.timestamp is None:
            continue
        elapsed = max(0.0, event.timestamp - encounter.start)
        last_elapsed = max(last_elapsed, elapsed)
        second = int(elapsed)
        if event.kind == 'COMBATANT_INFO' and len(event.fields) > 25:
            player_specs[event.fields[1]] = number(event.fields[25])
        if len(event.fields) > 2 and event.fields[1].startswith(('Player-', 'Pet-', 'Creature-')):
            if event.fields[2] not in ('', 'nil'):
                names[event.fields[1]] = event.fields[2]
        if len(event.fields) > 6 and event.fields[5].startswith(('Player-', 'Pet-', 'Creature-')):
            if event.fields[6] not in ('', 'nil'):
                names[event.fields[5]] = event.fields[6]
        snapshot = unit_snapshot(event)
        if snapshot:
            if snapshot.owner_guid.startswith('Player-'):
                pet_owners[snapshot.guid] = snapshot.owner_guid
            if snapshot.guid.startswith(('Player-', 'Creature-', 'Vehicle-')):
                position = Position(snapshot.x, snapshot.y, snapshot.map_id)
                unit_positions[second, snapshot.guid] = position
                exact_positions.setdefault(snapshot.guid, []).append((elapsed, position))
        if event.kind in ('SPELL_SUMMON', 'SPELL_CREATE') and len(event.fields) > 5:
            if event.fields[1].startswith('Player-') and event.fields[5].startswith(
                    ('Pet-', 'Creature-', 'Vehicle-')):
                pet_owners[event.fields[5]] = event.fields[1]
        effective_healing = healing_from_event(event)
        if effective_healing is not None and len(event.fields) > 1:
            heals.append((second, event.fields[1], effective_healing))
            if len(event.fields) > 6 and event.fields[5].startswith('Player-'):
                incoming_healing.setdefault(event.fields[5], []).append({
                    'time': elapsed, 'source_guid': event.fields[1],
                    'source': event.source or names.get(event.fields[1], event.fields[1]),
                    'ability_id': number(event.fields[9]) if len(event.fields) > 9 else 0,
                    'ability': event.ability, 'amount': effective_healing, 'absorbed': 0,
                })
        absorbed = absorb_from_event(event)
        if absorbed:
            shield_guid, shield_name, amount = absorbed
            names[shield_guid] = shield_name
            heals.append((second, shield_guid, amount))
            if len(event.fields) > 5 and event.fields[5].startswith('Player-'):
                incoming_healing.setdefault(event.fields[5], []).append({
                    'time': elapsed, 'source_guid': shield_guid, 'source': shield_name,
                    'ability_id': 0, 'ability': 'Absorb', 'amount': amount,
                    'absorbed': amount,
                })
        detail = damage_from_event(event)
        if detail and len(event.fields) > 6:
            guid = event.fields[5]
            if guid.startswith('Player-') and detail.snapshot_guid == guid and not event.kind.endswith(
                    '_LANDED'):
                ability_id = number(event.fields[9]) if event.kind.startswith(
                    ('SPELL_', 'RANGE_')) and len(event.fields) > 9 else 0
                incoming_damage.setdefault(guid, []).append({
                    'time': elapsed, 'source_guid': event.fields[1],
                    'source': event.source or names.get(event.fields[1], event.fields[1]),
                    'ability_id': ability_id, 'ability': event.ability,
                    'amount': detail.amount, 'effective': detail.effective,
                    'overkill': max(0, detail.overkill),
                    'health_before': detail.current_health + detail.effective,
                    'health_after': detail.current_health, 'max_health': detail.max_health,
                })
            if guid.startswith(('Creature-', 'Vehicle-')):
                unit = units.setdefault(guid, Unit(guid, event.target or guid))
                # SWING_DAMAGE carries the attacker's snapshot and is followed by
                # SWING_DAMAGE_LANDED with the target's snapshot. Count the hit
                # once, and only attach health when the snapshot identifies target.
                if not event.kind.endswith('_LANDED'):
                    unit.damage += detail.effective
                    damage[second, guid] = damage.get((second, guid), 0) + detail.effective
                    attacks.append((second, guid, event.fields[1], detail.effective))
                if detail.snapshot_guid == guid:
                    unit.max_health = max(unit.max_health, detail.max_health)
                    unit.observations += 1
                    health[second, guid] = (detail.current_health, detail.max_health)
        if event.kind in ('SPELL_CAST_START', 'SPELL_CAST_SUCCESS') and event.fields:
            guid = event.fields[1] if len(event.fields) > 1 else ''
            if guid.startswith(('Creature-', 'Vehicle-')):
                spell_id = number(event.fields[9]) if len(event.fields) > 9 else 0
                creature_casts.append((elapsed, guid, event.kind, event.ability, spell_id))
        if event.kind in ('SPELL_AURA_APPLIED', 'SPELL_AURA_APPLIED_DOSE',
                          'SPELL_AURA_REFRESH', 'SPELL_AURA_REMOVED') and len(event.fields) > 10:
            target_guid, source_guid = event.fields[5], event.fields[1]
            if target_guid.startswith('Player-'):
                key = (number(event.fields[9]), source_guid)
                if event.kind == 'SPELL_AURA_REMOVED':
                    active_debuffs.setdefault(target_guid, {}).pop(key, None)
                elif 'DEBUFF' in event.fields[11:]:
                    active_debuffs.setdefault(target_guid, {})[key] = {
                        'ability_id': key[0], 'ability': event.fields[10],
                        'source_guid': source_guid, 'source': event.source,
                        'applied_at': elapsed,
                    }
        if event.kind == 'UNIT_DIED' and len(event.fields) > 6:
            guid = event.fields[5]
            if guid.startswith('Player-'):
                player_deaths.append((second, event.target or guid))
                death_events.append((elapsed, guid, event.target or names.get(guid, guid)))
                debuffs_at_death[guid, len(death_events)] = list(
                    active_debuffs.get(guid, {}).values())

    # Bosses dominate maximum-health observations. The 15% threshold preserves
    # council encounters while excluding ordinary adds; damage breaks edge-case ties.
    largest = max((unit.max_health for unit in units.values()), default=0)
    bosses = [unit for unit in units.values()
              if unit.max_health >= largest * 0.15 and unit.observations > 0]
    if not bosses and units:
        bosses = [max(units.values(), key=lambda unit: (unit.max_health, unit.damage))]
    bosses.sort(key=lambda unit: (-unit.max_health, unit.name))
    boss_guids = {unit.guid for unit in bosses}
    boss_profiles = [BossProfile(
        unit.guid, unit.name,
        {second: position for (second, guid), position in unit_positions.items()
         if guid == unit.guid},
    ) for unit in bosses]

    pending_casts: dict[tuple[str, int], list[tuple[float, str]]] = {}
    mechanics: list[MechanicEvent] = []
    occurrences: dict[tuple[str, int], int] = {}
    previous_cast: dict[tuple[str, int], float] = {}

    def add_mechanic(start, end, guid, spell_id, ability, completed=True):
        key = (guid, spell_id)
        occurrences[key] = occurrences.get(key, 0) + 1
        previous = previous_cast.get(key)
        mechanics.append(MechanicEvent(
            start, end, guid, units[guid].name, spell_id, ability,
            occurrences[key], None if previous is None else start - previous, completed,
        ))
        previous_cast[key] = start

    for cast_time, guid, kind, ability, spell_id in creature_casts:
        if guid not in boss_guids:
            continue
        key = (guid, spell_id)
        if kind == 'SPELL_CAST_START':
            pending_casts.setdefault(key, []).append((cast_time, ability))
        else:
            pending = pending_casts.get(key, [])
            if pending:
                start, started_ability = pending.pop(0)
                add_mechanic(start, cast_time, guid, spell_id, started_ability or ability)
            else:
                add_mechanic(cast_time, cast_time, guid, spell_id, ability)
    for (guid, spell_id), pending in pending_casts.items():
        for start, ability in pending:
            add_mechanic(start, start + 3.0, guid, spell_id, ability, completed=False)
    mechanics.sort(key=lambda mechanic: mechanic.start)

    player_damage: dict[tuple[int, str], int] = {}
    player_healing: dict[tuple[int, str], int] = {}
    totals: dict[str, int] = {}
    healing_totals: dict[str, int] = {}
    for second, target_guid, source_guid, amount in attacks:
        if target_guid not in boss_guids:
            continue
        player_guid = source_guid if source_guid.startswith('Player-') else pet_owners.get(source_guid)
        if not player_guid:
            continue
        player_damage[second, player_guid] = player_damage.get((second, player_guid), 0) + amount
        totals[player_guid] = totals.get(player_guid, 0) + amount
    for second, source_guid, amount in heals:
        player_guid = source_guid if source_guid.startswith('Player-') else pet_owners.get(source_guid)
        if not player_guid:
            continue
        player_healing[second, player_guid] = player_healing.get((second, player_guid), 0) + amount
        healing_totals[player_guid] = healing_totals.get(player_guid, 0) + amount
    duration_seconds = max(encounter.duration if encounter.duration is not None else last_elapsed,
                           0.001)
    death_player_guids = {guid for _time, guid, _name in death_events}
    players = [Player(
        guid, names.get(guid, guid), totals.get(guid, 0), totals.get(guid, 0) / duration_seconds,
        class_name=SPEC_CLASSES.get(player_specs.get(guid), 'Unknown'),
        healing=healing_totals.get(guid, 0), hps=healing_totals.get(guid, 0) / duration_seconds,
        position_samples=sum(1 for _second, seen_guid in unit_positions if seen_guid == guid),
    ) for guid in totals.keys() | healing_totals.keys() | death_player_guids]
    players.sort(key=lambda player: (-player.damage, player.name))
    for rank, player in enumerate(players, 1):
        player.rank = rank
    healing_players = sorted((player for player in players if player.healing),
                             key=lambda player: (-player.healing, player.name))
    for rank, player in enumerate(healing_players, 1):
        player.healing_rank = rank
    player_guids = {player.guid for player in players}

    def position_before(guid, moment):
        return next(((seen, position) for seen, position in reversed(
            exact_positions.get(guid, [])) if seen <= moment), None)

    death_counts: dict[str, int] = {}
    deaths: list[DeathRecord] = []
    for death_index, (death_time, player_guid, player_name) in enumerate(death_events, 1):
        death_counts[player_guid] = death_counts.get(player_guid, 0) + 1
        history = [item for item in incoming_damage.get(player_guid, [])
                   if death_time - 30 <= item['time'] <= death_time]
        healing_history = [item for item in incoming_healing.get(player_guid, [])
                           if death_time - 30 <= item['time'] <= death_time]
        candidates = [item for item in history if death_time - item['time'] <= 2]
        lethal = candidates[-1] if candidates else None
        killing_blow = None if lethal is None else {
            'source_guid': lethal['source_guid'], 'source': lethal['source'],
            'ability_id': lethal['ability_id'], 'ability': lethal['ability'],
            'amount': lethal['amount'], 'overkill': lethal['overkill'],
            'provenance': 'inferred', 'confidence': 'moderate',
            'reason': 'Last observed damage event within two seconds before death.',
        }
        recent = [item for item in history if death_time - item['time'] <= 5]
        primary = max(recent, key=lambda item: item['amount'], default=lethal)
        primary_cause = None if primary is None else {
            'type': 'damage_event', 'source': primary['source'],
            'ability_id': primary['ability_id'], 'ability': primary['ability'],
            'amount': primary['amount'], 'provenance': 'inferred',
            'confidence': 'moderate',
            'reason': ('Largest observed damage event in the five seconds before death.'
                       if primary is not lethal else
                       'The inferred lethal event was also the strongest recent damage event.'),
            'contributing_events': [item for item in recent if item is not primary],
        }
        observed = position_before(player_guid, death_time)
        position_data = None
        spatial = {'boss_distances': [], 'distance_to_raid_center': None,
                   'nearby_players': [], 'players_within_5_yards': 0}
        if observed:
            seen_at, position = observed
            age = max(0.0, death_time - seen_at)
            confidence = 'high' if age < 0.5 else ('moderate' if age <= 2 else 'stale')
            position_data = {'x': position.x, 'y': position.y, 'map_id': position.map_id,
                             'observed_at': seen_at, 'sample_age_seconds': age,
                             'confidence': confidence}
            peers = []
            for guid in player_guids - {player_guid}:
                peer = position_before(guid, death_time)
                if peer and peer[1].map_id == position.map_id and death_time - peer[0] <= 2:
                    peers.append((guid, peer[1], math.hypot(
                        peer[1].x - position.x, peer[1].y - position.y)))
            if peers:
                center_x = sum(peer.x for _guid, peer, _distance in peers) / len(peers)
                center_y = sum(peer.y for _guid, peer, _distance in peers) / len(peers)
                spatial['distance_to_raid_center'] = math.hypot(
                    center_x - position.x, center_y - position.y)
                spatial['nearby_players'] = [
                    {'player_guid': guid, 'name': names.get(guid, guid), 'distance': distance}
                    for guid, _peer, distance in sorted(peers, key=lambda item: item[2])[:10]
                    if distance <= 10]
                spatial['players_within_5_yards'] = sum(
                    distance <= 5 for _guid, _peer, distance in peers)
            for boss_guid in boss_guids:
                boss = position_before(boss_guid, death_time)
                if boss and boss[1].map_id == position.map_id:
                    spatial['boss_distances'].append({
                        'boss_guid': boss_guid, 'boss': names.get(boss_guid, boss_guid),
                        'distance': math.hypot(boss[1].x - position.x,
                                               boss[1].y - position.y),
                        'sample_age_seconds': max(0.0, death_time - boss[0]),
                    })
        active_mechanics = [asdict(mechanic) for mechanic in mechanics
                            if mechanic.start <= death_time <= max(mechanic.end, mechanic.start)]
        deaths.append(DeathRecord(
            f'local-{encounter.attempt}:{player_guid}:{round(death_time * 1000)}',
            player_guid, player_name, next((p.class_name for p in players
                                            if p.guid == player_guid), 'Unknown'),
            death_counts[player_guid], death_time, killing_blow, primary_cause,
            history, healing_history, position_data, spatial, active_mechanics,
            debuffs_at_death.get((player_guid, death_index), [])))

    duration = encounter.duration
    final_second = max(0, int(duration if duration is not None else last_elapsed))
    notes: dict[int, list[str]] = {0: [f'Pull: {encounter.name}']}
    for cast_time, guid, kind, ability, _spell_id in creature_casts:
        if guid in boss_guids:
            label = 'begins' if kind == 'SPELL_CAST_START' else 'casts'
            notes.setdefault(int(cast_time), []).append(f'{units[guid].name} {label} {ability}')
    for second, name in player_deaths:
        notes.setdefault(second, []).append(f'{name} died')
    if encounter.end is not None:
        notes.setdefault(final_second, []).append('Kill' if encounter.success else 'Wipe')

    latest: dict[str, tuple[int, int]] = {}
    last_seen: dict[str, int] = {}
    latest_positions: dict[str, tuple[int, Position]] = {}
    latest_boss_positions: dict[str, tuple[int, Position]] = {}
    rows = []
    for second in range(final_second + 1):
        for guid in boss_guids:
            observation = health.get((second, guid))
            if observation:
                latest[guid] = observation
                last_seen[guid] = second
        # Encounters can replace one boss group with another between phases. A
        # short freshness window sums simultaneous council members without
        # carrying an inactive phase's old health through the rest of the fight.
        active = {guid: latest[guid] for guid in boss_guids
                  if guid in latest and second - last_seen[guid] <= 3}
        known = list(active.values())
        current = sum(item[0] for item in known) if known else None
        maximum = sum(item[1] for item in known) if known else None
        by_unit_damage = {guid: damage.get((second, guid), 0) for guid in boss_guids}
        dealt = sum(by_unit_damage.values())
        for guid in player_guids:
            observed_position = unit_positions.get((second, guid))
            if observed_position:
                latest_positions[guid] = (second, observed_position)
        current_positions = {
            guid: Position(position.x, position.y, position.map_id, second - observed_second)
            for guid, (observed_second, position) in latest_positions.items()
            if guid in player_guids
        }
        by_player_damage = {guid: player_damage.get((second, guid), 0)
                            for guid in player_guids}
        by_player_healing = {guid: player_healing.get((second, guid), 0)
                             for guid in player_guids}
        for guid in boss_guids:
            observed_position = unit_positions.get((second, guid))
            if observed_position:
                latest_boss_positions[guid] = (second, observed_position)
        current_boss_positions = {
            guid: Position(position.x, position.y, position.map_id, second - observed_second)
            for guid, (observed_second, position) in latest_boss_positions.items()
            if guid in active
        }
        rows.append(TimelineRow(second, current, maximum, dealt, notes.get(second, []),
                                active, by_unit_damage, by_player_damage, by_player_healing,
                                current_positions, current_boss_positions))
    position_history = {
        guid: [{'time': seen, 'x': position.x, 'y': position.y, 'map_id': position.map_id}
               for seen, position in observations]
        for guid, observations in exact_positions.items()
    }
    return EncounterTimeline(encounter, bosses, rows, players, boss_profiles, mechanics, deaths,
                             position_history)


def write_csv(timeline: EncounterTimeline, destination: Path):
    with destination.open('w', newline='', encoding='utf-8-sig') as output:
        writer = csv.writer(output)
        header = ['second', 'time', 'active_bosses', 'boss_health',
                  'boss_max_health', 'boss_health_pct', 'damage_to_boss', 'events']
        for boss in timeline.bosses:
            header.extend((f'{boss.name} health', f'{boss.name} max health',
                           f'{boss.name} health pct', f'{boss.name} damage'))
        writer.writerow(header)
        for row in timeline.rows:
            minute, second = divmod(row.second, 60)
            active_names = [boss.name for boss in timeline.bosses
                            if boss.guid in row.unit_health]
            values = [row.second, f'{minute}:{second:02d}', ' + '.join(active_names),
                      '' if row.health is None else row.health,
                      '' if row.max_health is None else row.max_health,
                      '' if row.health_pct is None else f'{row.health_pct:.3f}',
                      row.damage, ' | '.join(row.events)]
            for boss in timeline.bosses:
                observed = row.unit_health.get(boss.guid)
                if observed:
                    current, maximum = observed
                    values.extend((current, maximum, f'{100 * current / maximum:.3f}',
                                   row.unit_damage[boss.guid]))
                else:
                    values.extend(('', '', '', row.unit_damage[boss.guid]))
            writer.writerow(values)


def write_player_csv(timeline: EncounterTimeline, destination: Path):
    """Write a long-form player timeline suitable for filtering or plotting."""
    cumulative = {player.guid: 0 for player in timeline.players}
    with destination.open('w', newline='', encoding='utf-8-sig') as output:
        writer = csv.writer(output)
        writer.writerow(('damage_rank', 'healing_rank', 'player', 'class', 'player_guid', 'second', 'time',
                         'damage_this_second', 'cumulative_damage', 'total_damage',
                         'fight_dps', 'healing_this_second', 'cumulative_healing',
                         'total_effective_healing', 'fight_hps', 'x', 'y', 'map_id',
                         'position_age_seconds'))
        cumulative_healing = {player.guid: 0 for player in timeline.players}
        for row in timeline.rows:
            minute, second = divmod(row.second, 60)
            for player in timeline.players:
                dealt = row.player_damage.get(player.guid, 0)
                healed = row.player_healing.get(player.guid, 0)
                cumulative[player.guid] += dealt
                cumulative_healing[player.guid] += healed
                position = row.player_positions.get(player.guid)
                writer.writerow((
                    player.rank, player.healing_rank or '', player.name, player.class_name,
                    player.guid, row.second,
                    f'{minute}:{second:02d}', dealt, cumulative[player.guid],
                    player.damage, f'{player.dps:.2f}', healed, cumulative_healing[player.guid],
                    player.healing, f'{player.hps:.2f}',
                    '' if position is None else f'{position.x:.2f}',
                    '' if position is None else f'{position.y:.2f}',
                    '' if position is None else position.map_id,
                    '' if position is None else position.age,
                ))


def write_boss_position_csv(timeline: EncounterTimeline, destination: Path):
    """Write the observed coordinate profile for every inferred encounter boss."""
    with destination.open('w', newline='', encoding='utf-8-sig') as output:
        writer = csv.writer(output)
        writer.writerow(('encounter_id', 'encounter', 'attempt', 'boss', 'boss_guid',
                         'second', 'time', 'x', 'y', 'map_id'))
        for profile in timeline.boss_profiles:
            for second, position in sorted(profile.positions.items()):
                minute, display_second = divmod(second, 60)
                writer.writerow((timeline.encounter.encounter_id, timeline.encounter.name,
                                 timeline.encounter.attempt, profile.name, profile.guid,
                                 second, f'{minute}:{display_second:02d}',
                                 f'{position.x:.2f}', f'{position.y:.2f}', position.map_id))


def print_encounters(encounters: list[EncounterRef]):
    print('Attempt  Result  Duration  Difficulty  Encounter')
    for item in encounters:
        result = 'kill' if item.success else ('wipe' if item.end else 'open')
        duration = f'{item.duration:.1f}s' if item.duration is not None else '-'
        print(f'{item.attempt:>7}  {result:<6}  {duration:>8}  {item.difficulty:>10}  {item.name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--attempt', type=int, help='Attempt number to analyze')
    parser.add_argument('--output', type=Path, help='CSV path (defaults to timeline-N.csv)')
    args = parser.parse_args()
    encounters = scan_encounters(args.log)
    if args.attempt is None:
        print_encounters(encounters)
        return
    if not 1 <= args.attempt <= len(encounters):
        parser.error(f'attempt must be between 1 and {len(encounters)}')
    selected = encounters[args.attempt - 1]
    timeline = analyze_encounter(args.log, selected)
    output = args.output or Path(f'timeline-{args.attempt}.csv')
    write_csv(timeline, output)
    player_output = output.with_name(f'{output.stem}-players.csv')
    write_player_csv(timeline, player_output)
    boss_output = output.with_name(f'{output.stem}-boss-positions.csv')
    write_boss_position_csv(timeline, boss_output)
    boss_names = ', '.join(unit.name for unit in timeline.bosses) or 'none detected'
    print(f'Wrote {len(timeline.rows)} seconds to {output}')
    print(f'Wrote {len(timeline.players)} player timelines to {player_output}')
    print(f'Wrote {len(timeline.boss_profiles)} boss position profiles to {boss_output}')
    print(f'Boss units: {boss_names}')


if __name__ == '__main__':
    main()
