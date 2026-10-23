"""Deterministic placeholder mechanic scripts for confirmed live encounters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

if __package__:
    from .timer_export import TIMER_EXPORT_FORMAT, timer_export_to_script
else:  # Support direct execution from this directory.
    from timer_export import TIMER_EXPORT_FORMAT, timer_export_to_script


DEFAULT_SCRIPT = {
    'script_id': 'generic-placeholder-v1',
    'encounter_id': None,
    'encounter_name': 'Generic placeholder timeline',
    'difficulty': None,
    'placeholder': True,
    'encounter_triggers': {
        'start': {'id': 'fight-begin', 'event_type': 'ENCOUNTER_START',
                  'action': 'START_TIMELINE', 'establishes_time_zero': True},
        'end': {'id': 'fight-end', 'event_type': 'ENCOUNTER_END',
                'action': 'END_TIMELINE', 'cancel_pending_mechanics': True},
    },
    'mechanics': [
        {'id': 'start', 'time': 0, 'message': 'Boss encounter started.'},
        {'id': 'mechanic_1_warning', 'time': 10, 'message': 'Mechanic one approaching.'},
        {'id': 'mechanic_1', 'time': 20, 'message': 'Mechanic one activated.'},
        {'id': 'mechanic_2_warning', 'time': 35, 'message': 'Mechanic two approaching.'},
        {'id': 'mechanic_2', 'time': 45, 'message': 'Mechanic two activated.'},
        {'id': 'phase_change', 'time': 60, 'message': 'Encounter phase transition.'},
    ],
}


class SystemClock:
    def monotonic(self):
        return time.monotonic()

    def utc_now(self):
        return datetime.now(timezone.utc).isoformat()


class MechanicScriptRegistry:
    def __init__(self, scripts=None):
        supplied = scripts if scripts is not None else [DEFAULT_SCRIPT]
        self.scripts = [self._normalize(script) for script in supplied]
        self.source = None
        self.armed_script_id = None
        self._lock = threading.RLock()

    @classmethod
    def from_file(cls, path: Path | None):
        if path is None:
            return cls()
        path = Path(path)
        scripts = cls._read_scripts(path)
        registry = cls(scripts)
        registry.source = path
        return registry

    @staticmethod
    def _read_scripts(path):
        paths = sorted(path.glob('*.timer.json')) if path.is_dir() else [path]
        scripts = []
        for item in paths:
            data = json.loads(item.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('format') == TIMER_EXPORT_FORMAT:
                if data.get('enabled'):
                    scripts.append(timer_export_to_script(data))
                continue
            supplied = data.get('scripts') if isinstance(data, dict) else data
            if not isinstance(supplied, list):
                raise ValueError(
                    'Mechanic script file must contain a timer export, JSON list, or '
                    '{"scripts": [...]}.')
            scripts.extend(supplied)
        return scripts

    @staticmethod
    def _normalize(script):
        if not isinstance(script, dict):
            raise ValueError('Each mechanic script must be a JSON object.')
        if script.get('enabled') is False:
            raise ValueError('This mechanic script is a disabled draft; review and enable it first.')
        mechanics = script.get('mechanics', [])
        if not isinstance(mechanics, list) or len(mechanics) > 1000:
            raise ValueError('A mechanic script must contain at most 1,000 mechanics.')
        normalized = []
        seen = set()
        for item in mechanics:
            trigger = item.get('trigger', {}) if isinstance(item, dict) else {}
            scheduled = item.get('time', trigger.get('seconds')) if isinstance(item, dict) else None
            mechanic_id = str(item.get('id') or '') if isinstance(item, dict) else ''
            if not mechanic_id or mechanic_id in seen:
                raise ValueError('Mechanic IDs must be non-empty and unique within a script.')
            try:
                scheduled = float(scheduled)
            except (TypeError, ValueError) as error:
                raise ValueError(f'Mechanic {mechanic_id} has an invalid time.') from error
            if scheduled < 0:
                raise ValueError(f'Mechanic {mechanic_id} has a negative time.')
            seen.add(mechanic_id)
            normalized_item = {'id': mechanic_id, 'time': scheduled,
                               'message': str(item.get('message') or mechanic_id),
                               'kind': str(item.get('kind') or 'CALLOUT')}
            if item.get('ability_id') is not None:
                normalized_item['ability_id'] = int(item['ability_id'])
            if item.get('ability_ids'):
                normalized_item['ability_ids'] = [int(value) for value in item['ability_ids']]
            normalized.append(normalized_item)
        normalized.sort(key=lambda item: (item['time'], item['id']))
        encounter_id = (None if script.get('encounter_id', script.get('encounterId')) in
                        (None, 'placeholder', 'any') else
                        int(script.get('encounter_id', script.get('encounterId'))))
        difficulty = (None if script.get('difficulty') in (None, 'any') else
                      int(script.get('difficulty')))
        encounter_name = str(script.get('encounter_name') or
                             script.get('encounterName') or 'Configured timeline')
        supplied_triggers = script.get('encounter_triggers') or {}
        if not isinstance(supplied_triggers, dict):
            raise ValueError('encounter_triggers must be an object with start and end triggers.')

        def boundary_trigger(name, event_type, action):
            supplied = supplied_triggers.get(name) or {}
            if not isinstance(supplied, dict):
                raise ValueError(f'The {name} encounter trigger must be an object.')
            configured_event = str(supplied.get('event_type') or event_type).upper()
            if configured_event != event_type:
                raise ValueError(f'The {name} encounter trigger must use {event_type}.')
            trigger = {
                'id': str(supplied.get('id') or f'fight-{name}'),
                'event_type': configured_event,
                'action': str(supplied.get('action') or action).upper(),
                'match': {'encounter_id': encounter_id, 'difficulty': difficulty},
                'message': str(supplied.get('message') or
                               (supplied.get('emit') or {}).get('message') or (
                    f'{encounter_name} engaged. Timer started.' if name == 'start' else
                    f'{encounter_name} ended: {{result}}.')),
            }
            if name == 'start':
                trigger['establishes_time_zero'] = bool(
                    supplied.get('establishes_time_zero', True))
            else:
                trigger['cancel_pending_mechanics'] = bool(
                    supplied.get('cancel_pending_mechanics',
                                 supplied.get('cancel_pending_callouts', True)))
            return trigger

        return {
            'script_id': str(script.get('script_id') or script.get('id') or 'unnamed-script'),
            'encounter_id': encounter_id,
            'encounter_name': encounter_name,
            'difficulty': difficulty,
            'placeholder': bool(script.get('placeholder', True)),
            'encounter_triggers': {
                'start': boundary_trigger('start', 'ENCOUNTER_START', 'START_TIMELINE'),
                'end': boundary_trigger('end', 'ENCOUNTER_END', 'END_TIMELINE'),
            },
            'mechanics': normalized,
        }

    def select(self, encounter_id, difficulty):
        with self._lock:
            candidates = [script for script in self.scripts
                          if script['encounter_id'] in (None, int(encounter_id)) and
                          script['difficulty'] in (None, int(difficulty))]
            armed = next((script for script in candidates
                          if script['script_id'] == self.armed_script_id), None)
            if armed is not None:
                return armed
            if not candidates:
                return self._normalize(DEFAULT_SCRIPT)
            candidates.sort(key=lambda script: (
                script['encounter_id'] is None, script['difficulty'] is None,
                script['script_id']))
            return candidates[0]

    def reload(self):
        if self.source is None:
            return self.status()
        scripts = [self._normalize(script) for script in self._read_scripts(self.source)]
        with self._lock:
            self.scripts = scripts
            if not any(item['script_id'] == self.armed_script_id for item in scripts):
                self.armed_script_id = None
            return self.status()

    def arm(self, script_id=None, encounter_id=None, difficulty=None, reload=True):
        if reload:
            self.reload()
        with self._lock:
            candidates = [item for item in self.scripts if not item['placeholder']]
            if script_id:
                candidates = [item for item in candidates
                              if item['script_id'] == str(script_id)]
            if encounter_id is not None:
                candidates = [item for item in candidates
                              if item['encounter_id'] == int(encounter_id)]
            if difficulty is not None:
                candidates = [item for item in candidates
                              if item['difficulty'] in (None, int(difficulty))]
            if not candidates:
                raise ValueError('No enabled timer module matches that selection.')
            candidates.sort(key=lambda item: (
                item['difficulty'] is None, item['script_id']))
            self.armed_script_id = candidates[0]['script_id']
            return self.status()

    def disarm(self):
        with self._lock:
            self.armed_script_id = None
            return self.status()

    def status(self):
        with self._lock:
            modules = [{key: item[key] for key in (
                'script_id', 'encounter_id', 'encounter_name', 'difficulty', 'placeholder')}
                for item in self.scripts]
            return {'source': None if self.source is None else self.source.name,
                    'armed_script_id': self.armed_script_id, 'modules': modules}


@dataclass
class TimelineEncounter:
    session_id: str
    attempt_id: str
    attempt: int
    encounter_id: int
    encounter_name: str
    difficulty: int
    group_size: int
    log_start_timestamp: float


class EncounterTimelineEngine:
    """Advance a selected script with log time and a monotonic processing clock."""

    def __init__(self, registry=None, clock=None):
        self.registry = registry or MechanicScriptRegistry()
        self.clock = clock or SystemClock()
        self.reset()

    def reset(self):
        self.state = 'IDLE'
        self.encounter = None
        self.script = None
        self.started_monotonic = None
        self.last_log_timestamp = None
        self.next_index = 0
        self.events = []
        self.result = None

    def _base(self, event_type, elapsed):
        encounter = self.encounter
        return {
            'type': event_type, 'session_id': encounter.session_id,
            'attempt_id': encounter.attempt_id, 'attempt': encounter.attempt,
            'encounter_id': encounter.encounter_id,
            'encounter_name': encounter.encounter_name,
            'difficulty': encounter.difficulty, 'group_size': encounter.group_size,
            'elapsed_seconds': round(max(0.0, elapsed), 3),
            'log_start_timestamp': encounter.log_start_timestamp,
            'effective_log_timestamp': encounter.log_start_timestamp + max(0.0, elapsed),
            'processing_timestamp': self.clock.utc_now(),
            'script_id': self.script['script_id'],
            'placeholder': self.script['placeholder'],
        }

    def start(self, encounter):
        emitted = []
        if self.state == 'ACTIVE':
            emitted.extend(self.interrupt('superseded_by_new_encounter'))
        self.encounter = encounter
        self.script = self.registry.select(encounter.encounter_id, encounter.difficulty)
        self.started_monotonic = self.clock.monotonic()
        self.last_log_timestamp = encounter.log_start_timestamp
        self.next_index = 0
        self.events = []
        self.result = None
        self.state = 'ACTIVE'
        started = self._base('ENCOUNTER_STARTED', 0)
        started['status'] = 'STARTED'
        started['trigger'] = dict(self.script['encounter_triggers']['start'])
        self.events.append(started)
        emitted.append(started)
        emitted.extend(self.tick(encounter.log_start_timestamp))
        return emitted

    def elapsed(self, log_timestamp=None):
        if self.state != 'ACTIVE':
            return self.events[-1]['elapsed_seconds'] if self.events else 0.0
        if log_timestamp is not None:
            self.last_log_timestamp = max(self.last_log_timestamp or log_timestamp, log_timestamp)
        processing_elapsed = max(0.0, self.clock.monotonic() - self.started_monotonic)
        log_elapsed = max(0.0, (self.last_log_timestamp or
                               self.encounter.log_start_timestamp) -
                          self.encounter.log_start_timestamp)
        return max(processing_elapsed, log_elapsed)

    def tick(self, log_timestamp=None):
        if self.state != 'ACTIVE':
            return []
        elapsed = self.elapsed(log_timestamp)
        emitted = []
        mechanics = self.script['mechanics']
        while self.next_index < len(mechanics) and mechanics[self.next_index]['time'] <= elapsed:
            mechanic = mechanics[self.next_index]
            event = self._base('MECHANIC_EVENT', mechanic['time'])
            event.update({'mechanic_id': mechanic['id'], 'message': mechanic['message'],
                          'kind': mechanic.get('kind', 'CALLOUT'),
                          'scheduled_time': mechanic['time'], 'status': 'TRIGGERED',
                          'observed_elapsed_seconds': round(elapsed, 3)})
            if mechanic.get('ability_id') is not None:
                event['ability_id'] = mechanic['ability_id']
            if mechanic.get('ability_ids'):
                event['ability_ids'] = mechanic['ability_ids']
            self.events.append(event)
            emitted.append(event)
            self.next_index += 1
        return emitted

    def end(self, success, log_timestamp):
        if self.state != 'ACTIVE':
            return []
        emitted = self.tick(log_timestamp)
        elapsed = self.elapsed(log_timestamp)
        result = 'KILL' if success else 'WIPE'
        for mechanic in self.script['mechanics'][self.next_index:]:
            event = self._base('MECHANIC_EVENT', elapsed)
            event.update({'mechanic_id': mechanic['id'], 'message': mechanic['message'],
                          'kind': mechanic.get('kind', 'CALLOUT'),
                          'scheduled_time': mechanic['time'], 'status': 'CANCELED',
                          'reason': f'encounter_ended_{result.lower()}'})
            self.events.append(event)
            emitted.append(event)
        self.next_index = len(self.script['mechanics'])
        ended = self._base('ENCOUNTER_ENDED', elapsed)
        ended.update({'status': 'ENDED', 'result': result,
                      'log_end_timestamp': log_timestamp,
                      'trigger': dict(self.script['encounter_triggers']['end'])})
        self.events.append(ended)
        emitted.append(ended)
        self.state, self.result = 'COMPLETED', result
        return emitted

    def interrupt(self, reason):
        if self.state != 'ACTIVE':
            return []
        elapsed = self.elapsed()
        emitted = []
        for mechanic in self.script['mechanics'][self.next_index:]:
            event = self._base('MECHANIC_EVENT', elapsed)
            event.update({'mechanic_id': mechanic['id'], 'message': mechanic['message'],
                          'kind': mechanic.get('kind', 'CALLOUT'),
                          'scheduled_time': mechanic['time'], 'status': 'CANCELED',
                          'reason': reason})
            self.events.append(event)
            emitted.append(event)
        interrupted = self._base('ENCOUNTER_INTERRUPTED', elapsed)
        interrupted.update({'status': 'INTERRUPTED', 'reason': reason})
        self.events.append(interrupted)
        emitted.append(interrupted)
        self.next_index = len(self.script['mechanics'])
        self.state, self.result = 'COMPLETED', 'INTERRUPTED'
        return emitted

    def snapshot(self):
        next_mechanic = None
        if self.state == 'ACTIVE' and self.next_index < len(self.script['mechanics']):
            next_mechanic = dict(self.script['mechanics'][self.next_index])
        return {
            'state': self.state, 'script_id': self.script['script_id'] if self.script else None,
            'placeholder': self.script['placeholder'] if self.script else None,
            'elapsed_seconds': self.elapsed() if self.encounter else 0.0,
            'next_mechanic': next_mechanic, 'result': self.result,
            'events': [dict(event) for event in self.events],
        }
