"""Portable, bot-neutral export format for learned encounter timer drafts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path


TIMER_EXPORT_FORMAT = 'wow-fight-timer/v1'
TIMER_EXPORT_SCHEMA_VERSION = 1


def _trigger(value, phase, encounter_id, difficulty, encounter_name):
    value = value if isinstance(value, dict) else {}
    expected = 'ENCOUNTER_START' if phase == 'start' else 'ENCOUNTER_END'
    event_type = str(value.get('event_type') or expected).upper()
    if event_type != expected:
        raise ValueError(f'{phase} trigger must use {expected}.')
    default_message = (f'{encounter_name} engaged. Timer started.' if phase == 'start'
                       else f'{encounter_name} ended: {{result}}.')
    result = {
        'id': str(value.get('id') or f'fight-{phase}'),
        'event_type': event_type,
        'action': str(value.get('action') or
                      ('START_TIMELINE' if phase == 'start' else 'END_TIMELINE')),
        'match': {
            'encounter_id': int(encounter_id),
            'difficulty': difficulty,
        },
        'emit': {
            'type': 'FIGHT_ENGAGED' if phase == 'start' else 'FIGHT_ENDED',
            'message': str(value.get('message') or default_message),
        },
    }
    if phase == 'start':
        result['establishes_time_zero'] = bool(value.get('establishes_time_zero', True))
    else:
        result['cancel_pending_callouts'] = bool(
            value.get('cancel_pending_mechanics', True))
        result['result_values'] = ['KILL', 'WIPE', 'INTERRUPTED']
    return result


def build_timer_export(draft, enabled=None):
    """Convert an evidence-rich learner draft into a bounded runtime contract."""
    if not isinstance(draft, dict) or not isinstance(draft.get('script'), dict):
        raise ValueError('A learned timer draft with a script is required.')
    script = draft['script']
    encounter_id = script.get('encounter_id', draft.get('encounter_id'))
    if encounter_id is None:
        raise ValueError('The timer draft has no encounter ID.')
    encounter_name = str(script.get('encounter_name') or draft.get('encounter_name') or
                         f'Encounter {encounter_id}')
    difficulty = script.get('difficulty', draft.get('difficulty', 'any'))
    triggers = script.get('encounter_triggers') or {}
    start = _trigger(triggers.get('start'), 'start', encounter_id, difficulty, encounter_name)
    end = _trigger(triggers.get('end'), 'end', encounter_id, difficulty, encounter_name)

    callouts = []
    seen = set()
    for mechanic in script.get('mechanics') or []:
        mechanic_id = str(mechanic.get('id') or '')
        if not mechanic_id or mechanic_id in seen:
            raise ValueError('Timer callout IDs must be non-empty and unique.')
        try:
            offset = float(mechanic['time'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f'Timer callout {mechanic_id} has an invalid time.') from error
        if offset < 0:
            raise ValueError(f'Timer callout {mechanic_id} has a negative time.')
        seen.add(mechanic_id)
        item = {
            'id': mechanic_id,
            'offset_seconds': round(offset, 3),
            'kind': str(mechanic.get('kind') or 'CALLOUT'),
            'message': str(mechanic.get('message') or mechanic_id),
        }
        if mechanic.get('ability_id') is not None:
            item['ability_id'] = int(mechanic['ability_id'])
        if mechanic.get('ability_ids'):
            item['ability_ids'] = [int(value) for value in mechanic['ability_ids']]
        callouts.append(item)
    callouts.sort(key=lambda item: (item['offset_seconds'], item['id']))

    is_enabled = bool(script.get('enabled', False)) if enabled is None else bool(enabled)
    return {
        'format': TIMER_EXPORT_FORMAT,
        'schema_version': TIMER_EXPORT_SCHEMA_VERSION,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'timer_id': str(script.get('script_id') or draft.get('draft_id') or 'timer'),
        'status': ('REVIEWED_ENABLED' if is_enabled else
                   str(draft.get('status') or 'DRAFT_REQUIRES_REVIEW')),
        'enabled': is_enabled,
        'encounter': {
            'id': int(encounter_id), 'name': encounter_name, 'difficulty': difficulty,
        },
        'clock': {
            'unit': 'seconds', 'origin_event': 'ENCOUNTER_START',
            'stop_event': 'ENCOUNTER_END', 'overdue_policy': 'EMIT_DUE_IN_ORDER',
        },
        'triggers': {'start': start, 'end': end},
        'callouts': callouts,
        'provenance': {
            'learner_schema_version': draft.get('schema_version'),
            'generated_at': draft.get('generated_at'),
            'report_ids': list(draft.get('report_ids') or []),
            'pull_count': int(draft.get('pull_count') or 0),
            'source_fights': list(draft.get('source_fights') or []),
            'analysis': dict(draft.get('analysis') or {}),
            'guide_context': dict(draft.get('guide_context') or {}),
        },
    }


def timer_export_to_script(payload):
    """Convert one enabled portable timer module into the live-engine schema."""
    if not isinstance(payload, dict) or payload.get('format') != TIMER_EXPORT_FORMAT:
        raise ValueError(f'Timer module must use format {TIMER_EXPORT_FORMAT}.')
    if not payload.get('enabled'):
        raise ValueError('This timer module is disabled; review and enable it before live use.')
    encounter = payload.get('encounter') or {}
    triggers = payload.get('triggers') or {}
    callouts = payload.get('callouts') or []
    return {
        'script_id': str(payload.get('timer_id') or 'timer-module'),
        'encounter_id': int(encounter['id']),
        'encounter_name': str(encounter.get('name') or f"Encounter {encounter['id']}"),
        'difficulty': encounter.get('difficulty', 'any'),
        'placeholder': False,
        'enabled': True,
        'encounter_triggers': {
            'start': dict(triggers.get('start') or {}),
            'end': dict(triggers.get('end') or {}),
        },
        'mechanics': [{
            'id': item['id'], 'time': item['offset_seconds'],
            'kind': item.get('kind'), 'message': item.get('message'),
            **({'ability_id': item['ability_id']} if item.get('ability_id') is not None else {}),
            **({'ability_ids': item['ability_ids']} if item.get('ability_ids') else {}),
        } for item in callouts],
    }


def save_timer_export(payload, destination):
    """Atomically save a portable timer export."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.part')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)
    return destination
