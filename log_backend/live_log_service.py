"""Threaded, bounded ingestion for a configured local WoW combat log."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import threading
import time

if __package__:
    from .combat_log_core import decode_log_line, parse_event
    from .encounter_parser import analyze_encounter, scan_encounters, _timeline_from_dict, number
    from .mechanic_timeline import (EncounterTimelineEngine, MechanicScriptRegistry,
                                    TimelineEncounter)
else:  # Support direct execution from this directory.
    from combat_log_core import decode_log_line, parse_event
    from encounter_parser import analyze_encounter, scan_encounters, _timeline_from_dict, number
    from mechanic_timeline import (EncounterTimelineEngine, MechanicScriptRegistry,
                                   TimelineEncounter)


LIVE_SCHEMA_VERSION = 3
MAX_UPDATE_HISTORY = 5000
MAX_COMPLETED_ENCOUNTERS = 20


class LiveLogService:
    """Follow one owner-configured file and publish immutable analysis snapshots."""

    def __init__(self, source: Path | None, storage: Path, *, from_end=True,
                 poll_interval=0.2, rebuild_interval=0.5, mechanic_registry=None,
                 timeline_clock=None):
        self.source = Path(source).resolve() if source else None
        self.storage = Path(storage).resolve() / 'live-analysis'
        self.from_end = bool(from_end)
        self.poll_interval = max(0.05, float(poll_interval))
        self.rebuild_interval = max(0.05, float(rebuild_interval))
        source_key = str(self.source).casefold() if self.source else 'unconfigured'
        self.session_id = hashlib.sha256(source_key.encode()).hexdigest()[:16]
        self.source_name = self.source.name if self.source else None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0
        self._identity = None
        self._initialized = False
        self._cursor = 0
        self._updates = deque(maxlen=MAX_UPDATE_HISTORY)
        self._state = 'idle' if source else 'error'
        self._error_code = None if source else 'LIVE_SOURCE_NOT_CONFIGURED'
        self._parse_errors = 0
        self._last_event_at = None
        self._last_event_epoch = None
        self._last_rebuild = 0.0
        self._dirty = False
        self._active_spool = None
        self._active_ref = None
        self._timeline = None
        self._attempt = 0
        self._completed = deque(maxlen=MAX_COMPLETED_ENCOUNTERS)
        self._archived = {}
        self.timeline_engine = EncounterTimelineEngine(
            mechanic_registry or MechanicScriptRegistry(), timeline_clock)
        self._restored_schedule = None
        self._last_start_signature = None
        self._load_completed()

    @property
    def configured(self):
        return self.source is not None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='wow-live-log', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        with self._lock:
            if self._state == 'active':
                self._emit_timeline(self.timeline_engine.interrupt('monitor_stopped'))
                self._state = 'abandoned'
                self._persist_snapshot('abandoned')

    def _run(self):
        while not self._stop.wait(self.poll_interval):
            try:
                self.scan_once()
            except Exception:
                with self._lock:
                    self._state, self._error_code = 'error', 'LIVE_SOURCE_UNAVAILABLE'

    def _source_identity(self, stat):
        return (getattr(stat, 'st_dev', 0), getattr(stat, 'st_ino', 0))

    def scan_once(self):
        if self.source is None:
            return 0
        try:
            stat = self.source.stat()
        except OSError:
            with self._lock:
                self._error_code = 'LIVE_SOURCE_UNAVAILABLE'
            return 0
        with self._lock:
            if self._error_code == 'LIVE_SOURCE_UNAVAILABLE':
                self._error_code = None
        identity = self._source_identity(stat)
        if not self._initialized:
            self._offset = stat.st_size if self.from_end else 0
            self._identity, self._initialized = identity, True
        elif identity != self._identity or stat.st_size < self._offset:
            self._offset, self._identity = 0, identity
            with self._lock:
                if self._state == 'active':
                    self._emit_timeline(self.timeline_engine.interrupt('source_reset'))
                    self._state = 'abandoned'
                    self._persist_snapshot('abandoned')
                self._active_spool = self._active_ref = self._timeline = None
                self._last_start_signature = None
                self._state = 'idle'
            self._append_update('source_reset')
        processed = 0
        with self.source.open('rb') as stream:
            stream.seek(self._offset)
            while True:
                start = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b'\n'):
                    stream.seek(start)
                    break
                self._offset = stream.tell()
                processed += 1
                self._process_line(raw)
        self._emit_timeline(self.timeline_engine.tick())
        if self._dirty and self._active_spool and time.monotonic() - self._last_rebuild >= self.rebuild_interval:
            self._rebuild()
        return processed

    def _append_update(self, kind, event=None):
        with self._lock:
            self._cursor += 1
            item = {'cursor': self._cursor, 'type': kind,
                    'timestamp': datetime.now(timezone.utc).isoformat()}
            if event is not None:
                item.update({'event_type': event.kind, 'source': event.source,
                             'target': event.target, 'ability': event.ability})
                if self._active_ref and event.timestamp is not None:
                    item['fight_time_seconds'] = max(0.0, event.timestamp - self._active_ref.start)
            self._updates.append(item)

    def _emit_timeline(self, events):
        with self._lock:
            for event in events:
                self._cursor += 1
                item = dict(event)
                item['cursor'] = self._cursor
                self._updates.append(item)

    def _process_line(self, raw):
        event = parse_event(decode_log_line(raw).rstrip('\r\n'))
        if event.kind == 'UNPARSED' or event.timestamp is None:
            with self._lock:
                self._parse_errors += 1
            return
        self._last_event_at = datetime.fromtimestamp(event.timestamp, timezone.utc).isoformat()
        self._last_event_epoch = event.timestamp
        if event.kind == 'ENCOUNTER_START' and len(event.fields) >= 5:
            signature = (number(event.fields[1]), event.timestamp)
            if signature == self._last_start_signature:
                return
            with self._lock:
                if self._state == 'active':
                    self._emit_timeline(self.timeline_engine.interrupt(
                        'superseded_by_new_encounter'))
                    self._state = 'abandoned'
                    self._persist_snapshot('abandoned')
                self._attempt += 1
                self.storage.mkdir(parents=True, exist_ok=True)
                self._active_spool = self.storage / f'{self.session_id}-active.log'
                self._active_spool.write_bytes(raw)
                self._active_ref = None
                self._timeline = None
                self._state, self._error_code = 'active', None
                self._last_start_signature = signature
                self._restored_schedule = None
            timeline_encounter = TimelineEncounter(
                self.session_id, f'{self.session_id}-{self._attempt:04d}', self._attempt,
                number(event.fields[1]), event.fields[2], number(event.fields[3]),
                number(event.fields[4]), event.timestamp)
            self._emit_timeline(self.timeline_engine.start(timeline_encounter))
            self._rebuild()
            return
        if self._active_spool:
            with self._active_spool.open('ab') as output:
                output.write(raw)
            self._dirty = True
            self._append_update('combat_event', event)
            if event.kind != 'ENCOUNTER_END':
                self._emit_timeline(self.timeline_engine.tick(event.timestamp))
            if event.kind == 'UNIT_DIED':
                self._rebuild(force=True)
            if event.kind == 'ENCOUNTER_END':
                if (len(event.fields) < 6 or not self._active_ref or
                        number(event.fields[1]) != self._active_ref.encounter_id):
                    return
                success = len(event.fields) >= 6 and event.fields[5] == '1'
                self._emit_timeline(self.timeline_engine.end(success, event.timestamp))
                self._rebuild(force=True)
                with self._lock:
                    self._state = 'completed'
                    self._persist_snapshot('completed')
                    if self._timeline:
                        self._completed.append(self._timeline)
                    self._active_spool = None

    def _rebuild(self, force=False):
        spool = self._active_spool
        if not spool or not spool.is_file():
            return
        refs = scan_encounters(spool)
        if not refs:
            return
        ref = refs[-1]
        ref.attempt = self._attempt
        timeline = analyze_encounter(spool, ref)
        with self._lock:
            self._active_ref, self._timeline = ref, timeline
            self._last_rebuild = time.monotonic()
            self._dirty = False

    def _snapshot_path(self):
        return self.storage / f'{self.session_id}-latest.json.gz'

    def _persist_snapshot(self, state):
        if not self._timeline:
            return
        self.storage.mkdir(parents=True, exist_ok=True)
        payload = {'version': LIVE_SCHEMA_VERSION, 'session_id': self.session_id,
                   'state': state, 'attempt': self._attempt,
                   'timeline': asdict(self._timeline),
                   'scheduled_timeline': self.timeline_engine.snapshot()}
        destinations = [self._snapshot_path(), self.storage /
                        f'{self.session_id}-attempt-{self._attempt}.json.gz']
        for destination in destinations:
            temporary = destination.with_suffix(destination.suffix + '.part')
            with gzip.open(temporary, 'wt', encoding='utf-8') as output:
                json.dump(payload, output, ensure_ascii=False, separators=(',', ':'))
            temporary.replace(destination)
        self._archived[self._attempt] = {
            'state': state, 'timeline': self._timeline,
            'scheduled_timeline': payload['scheduled_timeline'],
        }
        attempts = sorted(self.storage.glob(f'{self.session_id}-attempt-*.json.gz'),
                          key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for expired in attempts[MAX_COMPLETED_ENCOUNTERS:]:
            expired.unlink(missing_ok=True)
        for attempt in sorted(self._archived)[:-MAX_COMPLETED_ENCOUNTERS]:
            del self._archived[attempt]

    def _load_completed(self):
        for path in sorted(self.storage.glob(f'{self.session_id}-attempt-*.json.gz')):
            try:
                with gzip.open(path, 'rt', encoding='utf-8') as source:
                    payload = json.load(source)
                if payload.get('version') == LIVE_SCHEMA_VERSION:
                    timeline = _timeline_from_dict(payload['timeline'])
                    attempt = int(payload.get('attempt') or timeline.encounter.attempt)
                    self._archived[attempt] = {
                        'state': str(payload.get('state') or 'completed'),
                        'timeline': timeline,
                        'scheduled_timeline': payload.get('scheduled_timeline'),
                    }
                    if payload.get('state') == 'completed':
                        self._completed.append(timeline)
                    self._attempt = max(self._attempt, attempt)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        path = self._snapshot_path()
        if path.is_file():
            try:
                with gzip.open(path, 'rt', encoding='utf-8') as source:
                    payload = json.load(source)
                if payload.get('version') == LIVE_SCHEMA_VERSION:
                    self._timeline = _timeline_from_dict(payload['timeline'])
                    self._attempt = max(self._attempt, int(payload.get('attempt') or 0))
                    self._state = str(payload.get('state') or 'completed')
                    self._restored_schedule = payload.get('scheduled_timeline')
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def completed(self):
        with self._lock:
            return list(self._completed)

    def archived(self):
        with self._lock:
            return dict(self._archived)

    def timeline_snapshot(self):
        with self._lock:
            snapshot = (self.timeline_engine.snapshot() if self.timeline_engine.encounter
                        else self._restored_schedule)
            return dict(snapshot) if snapshot else {
                'state': 'IDLE', 'script_id': None, 'placeholder': None,
                'elapsed_seconds': 0.0, 'next_mechanic': None,
                'result': None, 'events': [],
            }

    def snapshot(self):
        with self._lock:
            return self._timeline, self.status()

    def timer_module_status(self):
        result = self.timeline_engine.registry.status()
        result.update({'encounter_state': self._state,
                       'active_script_id': self.timeline_engine.snapshot().get('script_id')})
        return result

    def arm_timer_module(self, timer_id=None, encounter_id=None, difficulty=None):
        result = self.timeline_engine.registry.arm(
            timer_id, encounter_id, difficulty, reload=True)
        result.update({'encounter_state': self._state, 'applies_to': 'next_encounter'})
        return result

    def disarm_timer_module(self):
        result = self.timeline_engine.registry.disarm()
        result.update({'encounter_state': self._state, 'applies_to': 'automatic_selection'})
        return result

    def status(self):
        with self._lock:
            duration = None
            if self._timeline:
                duration = self._timeline.encounter.duration
                if duration is None and self._last_event_epoch is not None and self._active_ref:
                    duration = max(0.0, self._last_event_epoch - self._active_ref.start)
            return {
                'schema_version': LIVE_SCHEMA_VERSION, 'source_type': 'local_live_log',
                'session_id': self.session_id, 'source_name': self.source_name,
                'configured': self.configured, 'available': bool(self.source and self.source.is_file()),
                'encounter_state': self._state, 'cursor': self._cursor,
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'last_event_at': self._last_event_at, 'parse_errors': self._parse_errors,
                'fight_time_seconds': duration, 'complete': self._state == 'completed',
                'stale': bool(self._error_code), 'error_code': self._error_code,
                'mechanic_timeline': {
                    key: value for key, value in self.timeline_snapshot().items()
                    if key != 'events'
                },
                'timer_module': self.timer_module_status(),
            }

    def updates(self, since=0, limit=100):
        with self._lock:
            history = list(self._updates)
            if history and int(since) < history[0]['cursor'] - 1:
                return None, history[0]['cursor']
            matches = [item.copy() for item in history if item['cursor'] > int(since)]
            return matches[:limit], history[0]['cursor'] if history else self._cursor + 1
