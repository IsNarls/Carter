"""Validated, data-driven callouts for observed live combat-log events."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import threading


PROFILE_FORMAT = 'carter-observed-callouts/v1'
PROFILE_SCHEMA_VERSION = 1
DEFAULT_PROFILE_DIRECTORY = Path(__file__).resolve().parent / 'callout_profiles'
PROFILE_SUFFIX = '.callout.json'
MAX_PROFILE_BYTES = 256 * 1024
MAX_PROFILES = 500
MAX_MECHANICS = 200
MAX_TEXT_LENGTH = 240
MAX_POLICY_SECONDS = 300.0
PROFILE_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,79}$')
MECHANIC_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
CREATURE_GUID = re.compile(r'^Creature-(?:[^-]+-){4}(\d+)-[^-]+$')
SUPPORTED_EVENT_TYPES = {
    'SPELL_AURA_APPLIED',
    'SPELL_AURA_REMOVED',
    'SPELL_CAST_START',
    'SPELL_CAST_SUCCESS',
    'SPELL_SUMMON',
}
SUPPORTED_SOURCE_POLICIES = {'creature_allowlist', 'nil_or_zero_guid'}


def _positive_integer(value, field):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f'{field} must be a positive integer.')
    return value


def _bounded_number(value, field, *, maximum=MAX_POLICY_SECONDS):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0 or value > maximum):
        raise ValueError(f'{field} must be between 0 and {maximum:g}.')
    return float(value)


def _bounded_positive_number(value, field, *, maximum=MAX_POLICY_SECONDS):
    result = _bounded_number(value, field, maximum=maximum)
    if result <= 0:
        raise ValueError(f'{field} must be greater than zero.')
    return result


def _bounded_text(value, field, *, maximum=MAX_TEXT_LENGTH):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be text.')
    normalized = ' '.join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f'{field} must contain 1 through {maximum} characters.')
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(f'{field} contains unsupported control characters.')
    return normalized


def validate_callout_profile(payload):
    """Validate one bounded profile and return a normalized private copy."""
    if not isinstance(payload, dict) or payload.get('format') != PROFILE_FORMAT:
        raise ValueError(f'Callout profiles must use format {PROFILE_FORMAT}.')
    allowed_top = {
        'format', 'schema_version', 'profile_id', 'encounter_id', 'encounter_name',
        'difficulty_ids', 'boss_creature_ids', 'predictions_enabled', 'mechanics', 'profile_revision',
    }
    if set(payload) - allowed_top:
        raise ValueError('Callout profile contains unsupported top-level fields.')
    if payload.get('schema_version') != PROFILE_SCHEMA_VERSION:
        raise ValueError(
            f'Callout profile schema_version must be {PROFILE_SCHEMA_VERSION}.')
    profile_id = str(payload.get('profile_id') or '')
    if not PROFILE_ID.fullmatch(profile_id):
        raise ValueError('profile_id is invalid.')
    encounter_id = _positive_integer(payload.get('encounter_id'), 'encounter_id')
    encounter_name = _bounded_text(payload.get('encounter_name'), 'encounter_name', maximum=120)
    difficulties = payload.get('difficulty_ids')
    if (not isinstance(difficulties, list) or not difficulties
            or len(difficulties) > 20):
        raise ValueError('difficulty_ids must be a nonempty bounded list.')
    difficulties = [
        _positive_integer(value, 'difficulty_ids item') for value in difficulties
    ]
    if len(set(difficulties)) != len(difficulties):
        raise ValueError('difficulty_ids must be unique.')
    boss_ids = payload.get('boss_creature_ids')
    if not isinstance(boss_ids, list) or not boss_ids or len(boss_ids) > 40:
        raise ValueError('boss_creature_ids must be a nonempty bounded list.')
    boss_ids = [_positive_integer(value, 'boss_creature_ids item') for value in boss_ids]
    if len(set(boss_ids)) != len(boss_ids):
        raise ValueError('boss_creature_ids must be unique.')
    if payload.get('predictions_enabled') is not False:
        raise ValueError('predictions_enabled must be false; only observed events are supported.')
    mechanics = payload.get('mechanics')
    if (not isinstance(mechanics, list) or not mechanics
            or len(mechanics) > MAX_MECHANICS):
        raise ValueError(f'mechanics must contain 1 through {MAX_MECHANICS} entries.')

    normalized_mechanics = []
    mechanic_ids = set()
    for supplied in mechanics:
        if not isinstance(supplied, dict):
            raise ValueError('Every mechanic must be an object.')
        allowed_mechanic = {
            'mechanic_id', 'name', 'event_type', 'spell_id', 'source_policy',
            'grouping', 'duplicate_suppression_seconds', 'text', 'audio_key',
            'priority', 'max_lateness_seconds', 'confidence', 'wording_status',
        }
        if set(supplied) - allowed_mechanic:
            raise ValueError('Mechanic contains unsupported fields.')
        mechanic_id = str(supplied.get('mechanic_id') or '')
        if not MECHANIC_ID.fullmatch(mechanic_id) or mechanic_id in mechanic_ids:
            raise ValueError('mechanic_id values must be valid and unique.')
        mechanic_ids.add(mechanic_id)
        event_type = str(supplied.get('event_type') or '').upper()
        if event_type not in SUPPORTED_EVENT_TYPES:
            raise ValueError(f'Mechanic {mechanic_id} uses an unsupported event_type.')
        spell_id = _positive_integer(supplied.get('spell_id'), f'{mechanic_id}.spell_id')
        source = supplied.get('source_policy')
        if not isinstance(source, dict) or source.get('kind') not in SUPPORTED_SOURCE_POLICIES:
            raise ValueError(f'Mechanic {mechanic_id} has an invalid source_policy.')
        if source['kind'] == 'creature_allowlist':
            if set(source) != {'kind', 'creature_ids'}:
                raise ValueError(f'Mechanic {mechanic_id} source_policy has extra fields.')
            values = source.get('creature_ids')
            if not isinstance(values, list) or not values or len(values) > 40:
                raise ValueError(
                    f'Mechanic {mechanic_id} requires a bounded creature allowlist.')
            values = [_positive_integer(value, 'source creature ID') for value in values]
            if len(set(values)) != len(values):
                raise ValueError(f'Mechanic {mechanic_id} source IDs must be unique.')
            normalized_source = {'kind': source['kind'], 'creature_ids': values}
        else:
            if set(source) != {'kind', 'guids'}:
                raise ValueError(f'Mechanic {mechanic_id} source_policy has extra fields.')
            values = source.get('guids')
            allowed = {'nil', '0000000000000000'}
            if (not isinstance(values, list) or not values
                    or any(value not in allowed for value in values)):
                raise ValueError(f'Mechanic {mechanic_id} has invalid nil/zero GUIDs.')
            normalized_source = {'kind': source['kind'], 'guids': list(dict.fromkeys(values))}
        grouping = supplied.get('grouping')
        if (not isinstance(grouping, dict)
                or grouping.get('mode') != 'leading_edge_batch_window'):
            raise ValueError(f'Mechanic {mechanic_id} has an invalid grouping policy.')
        if set(grouping) != {'mode', 'window_seconds'}:
            raise ValueError(f'Mechanic {mechanic_id} grouping has extra fields.')
        priority = supplied.get('priority', 50)
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            raise ValueError(f'Mechanic {mechanic_id}.priority must be between 0 and 100.')
        normalized_mechanics.append({
            'mechanic_id': mechanic_id,
            'name': _bounded_text(supplied.get('name'), f'{mechanic_id}.name', maximum=120),
            'event_type': event_type,
            'spell_id': spell_id,
            'source_policy': normalized_source,
            'grouping': {
                'mode': 'leading_edge_batch_window',
                'window_seconds': _bounded_number(
                    grouping.get('window_seconds'), f'{mechanic_id}.grouping.window_seconds'),
            },
            'duplicate_suppression_seconds': _bounded_number(
                supplied.get('duplicate_suppression_seconds'),
                f'{mechanic_id}.duplicate_suppression_seconds'),
            'text': _bounded_text(supplied.get('text'), f'{mechanic_id}.text'),
            'audio_key': _bounded_text(
                supplied.get('audio_key'), f'{mechanic_id}.audio_key', maximum=100),
            'priority': priority,
            'max_lateness_seconds': _bounded_positive_number(
                supplied.get('max_lateness_seconds'), f'{mechanic_id}.max_lateness_seconds'),
            'confidence': _bounded_number(
                supplied.get('confidence', 1.0), f'{mechanic_id}.confidence', maximum=1.0),
            'wording_status': str(supplied.get('wording_status') or 'provisional'),
        })
        if normalized_mechanics[-1]['wording_status'] not in {'verified', 'provisional'}:
            raise ValueError(
                f'Mechanic {mechanic_id}.wording_status must be verified or provisional.')

    result = {
        'format': PROFILE_FORMAT,
        'schema_version': PROFILE_SCHEMA_VERSION,
        'profile_id': profile_id,
        'profile_revision': _positive_integer(payload.get('profile_revision', 1), 'profile_revision'),
        'encounter_id': encounter_id,
        'encounter_name': encounter_name,
        'difficulty_ids': difficulties,
        'boss_creature_ids': boss_ids,
        'predictions_enabled': False,
        'mechanics': normalized_mechanics,
    }
    index = {}
    for mechanic in normalized_mechanics:
        index.setdefault((mechanic['event_type'], mechanic['spell_id']), []).append(mechanic)
    result['_mechanic_index'] = index
    return result


class CalloutProfileRegistry:
    """Hot-reload a small profile catalog and select exact encounter matches."""

    def __init__(self, sources=None):
        self.sources = tuple(Path(value).resolve() for value in (
            sources if sources is not None else [DEFAULT_PROFILE_DIRECTORY]
        ))
        self._lock = threading.RLock()
        self._signature = None
        self._profiles = {}
        self._errors = []
        self.reload(force=True)

    def _paths(self):
        paths = []
        for source_index, source in enumerate(self.sources):
            if source.is_dir():
                paths.extend((source_index, item) for item in sorted(
                    source.glob(f'*{PROFILE_SUFFIX}')))
            elif source.is_file():
                paths.append((source_index, source))
        return paths

    def _source_signature(self, paths):
        signature = []
        for source_index, path in paths:
            try:
                stat = path.stat()
            except OSError:
                signature.append((source_index, str(path), None, None))
            else:
                signature.append((source_index, str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def reload(self, *, force=False):
        paths = self._paths()
        signature = self._source_signature(paths)
        with self._lock:
            if not force and signature == self._signature:
                return self.status()
        profiles = {}
        origins = {}
        errors = []
        for source_index, path in paths[:MAX_PROFILES]:
            try:
                if path.is_symlink():
                    raise ValueError('profile must not be a symbolic link')
                if path.stat().st_size > MAX_PROFILE_BYTES:
                    raise ValueError(f'profile exceeds {MAX_PROFILE_BYTES} bytes')
                payload = json.loads(path.read_text(encoding='utf-8'))
                profile = validate_callout_profile(payload)
                keys = [(profile['encounter_id'], difficulty)
                        for difficulty in profile['difficulty_ids']]
                conflict = next((origins[key] for key in keys
                                 if key in origins and origins[key][0] == source_index), None)
                if conflict is not None:
                    raise ValueError(
                        f'duplicate encounter/difficulty also defined by {conflict[1]}')
                for key in keys:
                    profiles[key] = profile
                    origins[key] = (source_index, path.name)
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
                errors.append({'file': path.name, 'reason': str(error)[:200]})
        if len(paths) > MAX_PROFILES:
            errors.append({
                'file': '*',
                'reason': f'profile catalog exceeds the {MAX_PROFILES}-file limit',
            })
        with self._lock:
            self._profiles = profiles
            self._errors = errors
            self._signature = signature
            return {
                'loaded_profiles': len({id(profile) for profile in profiles.values()}),
                'indexed_encounters': len(profiles),
                'errors': deepcopy(errors),
            }

    def select(self, encounter_id, difficulty, *, reload=True):
        if reload:
            self.reload()
        try:
            key = (int(encounter_id), int(difficulty))
        except (TypeError, ValueError):
            return None
        with self._lock:
            profile = self._profiles.get(key)
            return profile

    def status(self):
        with self._lock:
            unique = {id(profile) for profile in self._profiles.values()}
            return {
                'loaded_profiles': len(unique),
                'indexed_encounters': len(self._profiles),
                'errors': deepcopy(self._errors),
            }


def _creature_id(guid):
    match = CREATURE_GUID.fullmatch(str(guid or ''))
    return int(match.group(1)) if match else None


def _event_details(event):
    fields = event.fields
    if len(fields) <= 10:
        return None
    try:
        spell_id = int(fields[9])
    except (TypeError, ValueError):
        return None
    source_guid = str(fields[1] or '')
    return {
        'event_type': event.kind,
        'spell_id': spell_id,
        'source_guid': source_guid,
        'source_creature_id': _creature_id(source_guid),
        'destination_guid': str(fields[5] or '') if len(fields) > 5 else '',
    }


def _matches_source(details, policy):
    if policy['kind'] == 'nil_or_zero_guid':
        return details['source_guid'] in policy['guids']
    return details['source_creature_id'] in policy['creature_ids']


class ObservedCalloutEngine:
    """Match trusted observed-event profiles for one active encounter at a time."""

    def __init__(self, registry=None):
        self.registry = registry or CalloutProfileRegistry()
        self.reset()

    def reset(self):
        self.active = None

    def start(self, encounter):
        self.reset()
        profile = self.registry.select(encounter.encounter_id, encounter.difficulty)
        if profile is None:
            return None
        self.active = {
            'profile': profile,
            'session_id': encounter.session_id,
            'attempt_id': encounter.attempt_id,
            'attempt': encounter.attempt,
            'encounter_id': encounter.encounter_id,
            'encounter_name': encounter.encounter_name,
            'difficulty': encounter.difficulty,
            'started_at': encounter.log_start_timestamp,
            'occurrences': {},
            'group_starts': {},
            'last_emitted_at': {},
            'seen_fingerprints': set(),
        }
        return profile

    def process(self, event, *, suppress_emission=False):
        active = self.active
        if active is None or event.timestamp is None:
            return []
        details = _event_details(event)
        if details is None:
            return []
        mechanics = active['profile']['_mechanic_index'].get(
            (details['event_type'], details['spell_id']), ())
        elapsed = event.timestamp - active['started_at']
        if elapsed < 0:
            return []
        emitted = []
        for mechanic in mechanics:
            if not _matches_source(details, mechanic['source_policy']):
                continue
            fingerprint = (
                details['event_type'], details['spell_id'], event.timestamp,
                details['source_guid'], details['destination_guid'],
            )
            if fingerprint in active['seen_fingerprints']:
                continue
            active['seen_fingerprints'].add(fingerprint)
            mechanic_id = mechanic['mechanic_id']
            group_start = active['group_starts'].get(mechanic_id)
            if (group_start is not None
                    and event.timestamp - group_start
                    <= mechanic['grouping']['window_seconds']):
                continue
            active['group_starts'][mechanic_id] = event.timestamp
            last_emitted = active['last_emitted_at'].get(mechanic_id)
            if (last_emitted is not None
                    and elapsed - last_emitted <= mechanic['duplicate_suppression_seconds']):
                continue
            occurrence = active['occurrences'].get(mechanic_id, 0) + 1
            active['occurrences'][mechanic_id] = occurrence
            active['last_emitted_at'][mechanic_id] = elapsed
            if suppress_emission:
                continue
            emitted.append({
                'type': 'MECHANIC_CALLOUT',
                'session_id': active['session_id'],
                'attempt_id': active['attempt_id'],
                'attempt': active['attempt'],
                'encounter_id': active['encounter_id'],
                'encounter_name': active['encounter_name'],
                'difficulty': active['difficulty'],
                'profile_id': active['profile']['profile_id'],
                'mechanic_id': mechanic_id,
                'occurrence': occurrence,
                'message': mechanic['text'],
                'audio_key': mechanic['audio_key'],
                'priority': mechanic['priority'],
                'confidence': mechanic['confidence'],
                'wording_status': mechanic['wording_status'],
                'elapsed_seconds': round(elapsed, 3),
                'effective_log_timestamp': event.timestamp,
                'max_lateness_seconds': mechanic['max_lateness_seconds'],
                'observed_event_type': details['event_type'],
                'spell_id': details['spell_id'],
                'source_creature_id': details['source_creature_id'],
            })
        return emitted

    def end(self):
        self.reset()

    def status(self):
        profile = self.active['profile'] if self.active else None
        result = self.registry.status()
        result['active_profile_id'] = profile['profile_id'] if profile else None
        result['active_encounter_id'] = profile['encounter_id'] if profile else None
        return result


def main():
    parser = argparse.ArgumentParser(
        description='Validate Carter observed live-callout profiles.')
    parser.add_argument('sources', nargs='*', type=Path,
                        help='Profile file or directory (defaults to packaged profiles)')
    args = parser.parse_args()
    registry = CalloutProfileRegistry(args.sources or [DEFAULT_PROFILE_DIRECTORY])
    status = registry.status()
    if status['errors']:
        for error in status['errors']:
            print(f"Invalid {error['file']}: {error['reason']}")
        return 1
    print(
        f"Valid {PROFILE_FORMAT}: {status['loaded_profiles']} profile(s), "
        f"{status['indexed_encounters']} encounter/difficulty match(es).")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
