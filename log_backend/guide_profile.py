"""Validated guide context for conservative combat-log timer learning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


GUIDE_PROFILE_FORMAT = 'wow-guide-profile/v1'
GUIDE_PROFILE_SCHEMA_VERSION = 1
ALLOWED_TIMER_MODES = {'observed_static', 'conditional', 'reactive', 'phase', 'never'}
NAME_TOKEN = re.compile(r'[^a-z0-9]+')


def normalize_mechanic_name(value):
    """Normalize guide and log ability names for conservative exact matching."""
    return NAME_TOKEN.sub('', str(value).casefold())


def validate_guide_profile(payload):
    """Validate the bounded guide-profile contract and return it unchanged."""
    if not isinstance(payload, dict) or payload.get('format') != GUIDE_PROFILE_FORMAT:
        raise ValueError(f'Guide profile must use format {GUIDE_PROFILE_FORMAT}.')
    if payload.get('schema_version') != GUIDE_PROFILE_SCHEMA_VERSION:
        raise ValueError(f'Guide profile schema_version must be {GUIDE_PROFILE_SCHEMA_VERSION}.')
    for field in ('profile_id', 'title', 'source', 'timing_policy', 'phases', 'mechanics'):
        if not payload.get(field):
            raise ValueError(f'Guide profile requires {field}.')
    source = payload['source']
    if not isinstance(source, dict) or not source.get('content_sha256'):
        raise ValueError('Guide profile source requires content_sha256 provenance.')
    phases = payload['phases']
    mechanics = payload['mechanics']
    if not isinstance(phases, list) or not isinstance(mechanics, list):
        raise ValueError('Guide profile phases and mechanics must be arrays.')
    timing_policy = payload['timing_policy']
    if (timing_policy.get('encounter_schedule_source') != 'observed_logs_only' or
            timing_policy.get('effect_intervals_are_cast_intervals') is not False or
            timing_policy.get('guide_context_can_enable_timer') is not False or
            timing_policy.get('damage_values_included') is not False):
        raise ValueError('Guide timing policy must keep logs authoritative and exclude damage values.')
    phase_ids = {str(item.get('id') or '') for item in phases if isinstance(item, dict)}
    if '' in phase_ids or len(phase_ids) != len(phases):
        raise ValueError('Guide phase IDs must be non-empty and unique.')
    mechanic_ids = set()
    mechanic_names = set()
    for mechanic in mechanics:
        if not isinstance(mechanic, dict):
            raise ValueError('Every guide mechanic must be an object.')
        mechanic_id = str(mechanic.get('id') or '')
        name = str(mechanic.get('name') or '')
        if not mechanic_id or mechanic_id in mechanic_ids:
            raise ValueError('Guide mechanic IDs must be non-empty and unique.')
        if not name or not normalize_mechanic_name(name):
            raise ValueError(f'Guide mechanic {mechanic_id} requires a name.')
        mechanic_ids.add(mechanic_id)
        names = [name, *(mechanic.get('aliases') or [])]
        for candidate in names:
            normalized = normalize_mechanic_name(candidate)
            if normalized in mechanic_names:
                raise ValueError(f'Duplicate guide mechanic name or alias: {candidate}.')
            mechanic_names.add(normalized)
        unknown_phases = set(mechanic.get('phases') or []) - phase_ids
        if unknown_phases:
            raise ValueError(f'Guide mechanic {mechanic_id} references unknown phases.')
        timer_mode = (mechanic.get('timer_policy') or {}).get('mode')
        if timer_mode not in ALLOWED_TIMER_MODES:
            raise ValueError(f'Guide mechanic {mechanic_id} has an invalid timer mode.')
        forbidden_fact_keys = {'damage', 'damage_amount', 'damage_value', 'amount'}
        if forbidden_fact_keys.intersection(mechanic.get('facts') or {}):
            raise ValueError(f'Guide mechanic {mechanic_id} includes a forbidden damage value.')
    for mechanic in mechanics:
        for relation in mechanic.get('relationships') or []:
            if relation.get('target') not in mechanic_ids:
                raise ValueError(
                    f"Guide mechanic {mechanic['id']} references an unknown relationship target.")
    return payload


def load_guide_profile(value):
    """Load one guide profile from a mapping or JSON path."""
    if isinstance(value, dict):
        return validate_guide_profile(value)
    path = Path(value)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Could not read guide profile {path}: {error}') from error
    return validate_guide_profile(payload)


def guide_mechanic_index(profile):
    """Index names and aliases while preserving one unambiguous mechanic per key."""
    profile = validate_guide_profile(profile)
    result = {}
    for mechanic in profile['mechanics']:
        for name in [mechanic['name'], *(mechanic.get('aliases') or [])]:
            result[normalize_mechanic_name(name)] = mechanic
    return result


def annotate_timer_candidates(candidates, profile):
    """Attach guide context by ability name without changing learner acceptance."""
    profile = validate_guide_profile(profile)
    index = guide_mechanic_index(profile)
    matched = []
    for candidate in candidates:
        mechanic = index.get(normalize_mechanic_name(candidate.get('ability')))
        if mechanic is None:
            continue
        context = {
            'profile_id': profile['profile_id'],
            'mechanic_id': mechanic['id'],
            'name': mechanic['name'],
            'matched_by': 'normalized_ability_name',
            'actors': list(mechanic.get('actors') or []),
            'phases': list(mechanic.get('phases') or []),
            'difficulties': list(mechanic.get('difficulties') or []),
            'categories': list(mechanic.get('categories') or []),
            'severity': mechanic.get('severity'),
            'timer_policy': dict(mechanic.get('timer_policy') or {}),
            'facts': dict(mechanic.get('facts') or {}),
        }
        candidate['guide_context'] = context
        matched.append(mechanic['id'])
    return {
        'format': GUIDE_PROFILE_FORMAT,
        'profile_id': profile['profile_id'],
        'title': profile['title'],
        'source': dict(profile['source']),
        'policy': 'context_only_observed_logs_remain_authoritative',
        'matched_mechanic_ids': sorted(set(matched)),
        'matched_candidate_count': len(matched),
        'total_guide_mechanics': len(profile['mechanics']),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    args = parser.parse_args()
    profile = load_guide_profile(args.profile)
    print(f"Valid {GUIDE_PROFILE_FORMAT}: {profile['title']} "
          f"({len(profile['mechanics'])} mechanics, {len(profile['phases'])} phases)")


if __name__ == '__main__':
    main()
