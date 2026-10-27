"""Threaded, bounded ingestion for a configured local WoW combat log."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

if __package__:
    from .combat_log_core import decode_log_line, parse_event
    from .encounter_parser import analyze_encounter, scan_encounters, _timeline_from_dict, number
    from .live_callouts import (CalloutProfileRegistry, DEFAULT_PROFILE_DIRECTORY,
                                ObservedCalloutEngine)
    from .mechanic_timeline import (EncounterTimelineEngine, MechanicScriptRegistry,
                                    TimelineEncounter)
else:  # Support direct execution from this directory.
    from combat_log_core import decode_log_line, parse_event
    from encounter_parser import analyze_encounter, scan_encounters, _timeline_from_dict, number
    from live_callouts import (CalloutProfileRegistry, DEFAULT_PROFILE_DIRECTORY,
                               ObservedCalloutEngine)
    from mechanic_timeline import (EncounterTimelineEngine, MechanicScriptRegistry,
                                   TimelineEncounter)


LIVE_SCHEMA_VERSION = 3
MAX_UPDATE_HISTORY = 5000
MAX_LIFECYCLE_HISTORY = 200
MAX_COMPLETED_ENCOUNTERS = 20
MAX_FUTURE_CALLOUT_SECONDS = 2.0
LIVE_ACCESS_TOKEN_FILENAME = '.live-access-token'
LIVE_ACCESS_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{40,128}$')
REMOTE_INGEST_STATE_FILENAME = 'remote-ingest-state.json'


def live_access_token_info(storage: Path, *, create=False):
    """Return non-secret token metadata, optionally creating the private token."""
    directory = Path(storage).resolve() / 'live-analysis'
    path = directory / LIVE_ACCESS_TOKEN_FILENAME
    if not create and not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('Live access token path must not be a symbolic link.')
    created = False
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = None
        else:
            created = True
            with os.fdopen(descriptor, 'w', encoding='ascii') as output:
                output.write(token)
                output.flush()
                os.fsync(output.fileno())
    try:
        value = path.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError) as error:
        raise ValueError('The live access token could not be read safely.') from error
    if not LIVE_ACCESS_TOKEN_PATTERN.fullmatch(value):
        raise ValueError('The live access token file is invalid; repair it manually.')
    try:
        path.chmod(0o600)
    except OSError as error:
        raise ValueError('The live access token permissions could not be secured.') from error
    return {
        'path': path,
        'fingerprint': hashlib.sha256(value.encode('ascii')).hexdigest()[:12],
        'created': created,
    }


def ensure_live_access_token(storage: Path):
    """Create or reuse the future live-access token without exposing its value."""
    return live_access_token_info(storage, create=True)


class LiveLogService:
    """Follow one owner-configured file and publish immutable analysis snapshots."""

    def __init__(self, source: Path | None, storage: Path, *, from_end=True,
                 poll_interval=0.2, rebuild_interval=0.5, mechanic_registry=None,
                 timeline_clock=None, remote=False, callout_registry=None):
        self.source = Path(source).resolve() if source else None
        self.remote = bool(remote)
        storage_root = Path(storage).resolve()
        self.storage = storage_root / 'live-analysis'
        token = ensure_live_access_token(storage_root)
        self.access_token_path = token['path']
        self.access_token_fingerprint = token['fingerprint']
        self.from_end = bool(from_end)
        self.poll_interval = max(0.05, float(poll_interval))
        self.rebuild_interval = max(0.05, float(rebuild_interval))
        source_key = str(self.source).casefold() if self.source else (
            f'remote:{Path(storage).resolve()}' if self.remote else 'unconfigured')
        self.session_id = hashlib.sha256(source_key.encode()).hexdigest()[:16]
        # session_id intentionally identifies the configured source so persisted
        # snapshots keep stable names.  feed_id identifies this in-memory
        # producer generation so polling clients can safely detect a restart
        # even when its cursor has already caught up to the old value.
        self.feed_id = secrets.token_hex(16)
        self.source_name = self.source.name if self.source else (
            'remote WoWCombatLog.txt' if self.remote else None)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0
        self._identity = None
        self._initialized = False
        self._cursor = 0
        self._updates = deque(maxlen=MAX_UPDATE_HISTORY)
        # Preserve rare encounter transitions separately. A fast remote sender
        # can upload more than MAX_UPDATE_HISTORY combat lines between Carter's
        # polls; lifecycle events must remain observable across that churn.
        self._lifecycle_updates = deque(maxlen=MAX_LIFECYCLE_HISTORY)
        self._state = 'idle' if source or self.remote else 'error'
        self._error_code = None if source or self.remote else 'LIVE_SOURCE_NOT_CONFIGURED'
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
        self.callout_engine = ObservedCalloutEngine(
            callout_registry or CalloutProfileRegistry((
                DEFAULT_PROFILE_DIRECTORY,
                storage_root / 'callout-profiles',
            )))
        self._restored_schedule = None
        self._last_start_signature = None
        self._remote_streams = self._load_remote_ingest_state() if self.remote else {}
        self._sender_diagnostics = None
        self._test_announcement_ids = set()
        self._test_announcement_order = deque(maxlen=200)
        self._load_completed()
        if self.remote:
            self._recover_remote_spool()

    @property
    def configured(self):
        return self.source is not None or self.remote

    def _remote_ingest_state_path(self):
        return self.storage / REMOTE_INGEST_STATE_FILENAME

    def _load_remote_ingest_state(self):
        try:
            payload = json.loads(self._remote_ingest_state_path().read_text(encoding='utf-8'))
            streams = payload.get('streams', {})
            if isinstance(streams, dict):
                return {str(key): int(value) for key, value in streams.items()
                        if isinstance(value, int) and value >= 0}
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_remote_ingest_state(self):
        self.storage.mkdir(parents=True, exist_ok=True)
        destination = self._remote_ingest_state_path()
        temporary = destination.with_suffix('.json.part')
        temporary.write_text(json.dumps({'version': 1, 'streams': self._remote_streams},
                                        separators=(',', ':')), encoding='utf-8')
        temporary.replace(destination)

    def _record_sender_diagnostics(self, diagnostics, offset, expected):
        if not diagnostics:
            return
        safe = {
            key: value for key, value in diagnostics.items()
            if key in {
                'state', 'thread_alive', 'file_size', 'reader_offset', 'pending_bytes',
                'last_attempt_age_ms', 'last_success_age_ms', 'last_result',
                'client_version',
            }
        }
        safe.update({
            'sender_offset': offset,
            'server_expected_offset': offset if expected is None else expected,
            'offset_matches': expected is None or expected == offset,
            'received_at': datetime.now(timezone.utc).isoformat(),
        })
        self._sender_diagnostics = safe

    def _emit_test_announcement(self, request_id):
        if request_id in self._test_announcement_ids:
            return False
        if len(self._test_announcement_order) >= self._test_announcement_order.maxlen:
            expired = self._test_announcement_order.popleft()
            self._test_announcement_ids.discard(expired)
        self._test_announcement_ids.add(request_id)
        self._test_announcement_order.append(request_id)
        now = datetime.now(timezone.utc)
        self._cursor += 1
        item = {
            'cursor': self._cursor,
            'type': 'ANNOUNCEMENT_TEST_REQUESTED',
            'session_id': self.session_id,
            'attempt_id': f'test-{request_id}',
            'request_id': request_id,
            'timestamp': now.isoformat(),
            'received_at': now.isoformat(),
            'event_age_seconds': 0.0,
        }
        self._updates.append(item)
        self._lifecycle_updates.append(item.copy())
        return True

    def ingest_remote_batch(self, stream_id, offset, payload, diagnostics=None,
                            test_announcement_id=None):
        """Ingest one complete-line byte batch and durably acknowledge its file offset."""
        if not self.remote:
            raise ValueError('Remote ingestion is not configured.')
        stream_id = str(stream_id or '')
        if not re.fullmatch(r'[A-Za-z0-9._-]{8,128}', stream_id):
            raise ValueError('The stream identifier is invalid.')
        try:
            offset = int(offset)
        except (TypeError, ValueError) as error:
            raise ValueError('The stream offset is invalid.') from error
        if offset < 0:
            raise ValueError('The stream offset is invalid.')
        if payload and not payload.endswith(b'\n'):
            raise ValueError('Live log payloads must end with a complete line.')
        test_announcement_id = str(test_announcement_id or '') or None
        if test_announcement_id and not re.fullmatch(
                r'[0-9a-f]{32}', test_announcement_id):
            raise ValueError('The live announcement test identifier is invalid.')
        if payload and test_announcement_id:
            raise ValueError('A live announcement test request must have an empty body.')
        if not payload and not diagnostics and not test_announcement_id:
            raise ValueError('An empty live request must contain safe diagnostics or a test request.')
        with self._lock:
            expected = self._remote_streams.get(stream_id)
            self._record_sender_diagnostics(diagnostics, offset, expected)
            if not payload:
                if expected is not None and offset != expected:
                    return {
                        'accepted': 0, 'next_offset': expected,
                        'offset_mismatch': True, 'heartbeat': True,
                    }
                test_created = (self._emit_test_announcement(test_announcement_id)
                                if test_announcement_id else False)
                response = {
                    'accepted': 0, 'next_offset': offset,
                    'duplicate': False, 'heartbeat': True,
                    'diagnostics_received': bool(diagnostics),
                }
                if test_announcement_id:
                    response.update({
                        'test_announcement_queued': test_created,
                        'test_announcement_duplicate': not test_created,
                    })
                return response
            end = offset + len(payload)
            if expected is not None and end <= expected:
                # Repair a state write that may have failed after the batch was
                # already accepted in memory, before acknowledging the retry.
                self._save_remote_ingest_state()
                return {'accepted': 0, 'next_offset': expected, 'duplicate': True}
            if expected is not None and offset != expected:
                return {'accepted': 0, 'next_offset': expected, 'offset_mismatch': True}
            processed = 0
            spool_output = None
            spool_path = None
            try:
                for raw in payload.splitlines(keepends=True):
                    # Opening and closing the active spool once per combat event
                    # cannot keep up with dense raid logs.  Reuse one buffered
                    # append handle for ordinary lines, but close it around
                    # encounter boundaries so start/end snapshot work always
                    # sees every preceding byte and can rotate the spool safely.
                    boundary = (b'ENCOUNTER_START' in raw
                                or b'ENCOUNTER_END' in raw)
                    active_spool = self._active_spool
                    if boundary or active_spool != spool_path:
                        if spool_output is not None:
                            spool_output.close()
                        spool_output = None
                        spool_path = None
                    if (not boundary and active_spool is not None
                            and spool_output is None):
                        spool_output = self._open_spool_append(active_spool)
                        spool_path = active_spool
                    self._process_line(raw, spool_output=spool_output)
                    processed += 1
            finally:
                if spool_output is not None:
                    spool_output.close()
            # Do not synchronously re-analyze the growing spool for every
            # remote batch. Raid backlogs can be hundreds of MiB, and a full
            # rebuild here delays the acknowledgement long enough for the
            # sender to time out and retry. ENCOUNTER_START creates the initial
            # snapshot and the authoritative ENCOUNTER_END path performs the
            # final rebuild/persistence; lifecycle updates are emitted while
            # processing their individual lines.
            self._remote_streams[stream_id] = end
            # Retain only a small rotation history.
            if len(self._remote_streams) > 8:
                for expired in list(self._remote_streams)[:-8]:
                    del self._remote_streams[expired]
            self._save_remote_ingest_state()
            return {'accepted': processed, 'next_offset': end, 'duplicate': False}

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
                if self.remote:
                    # Remote input has no source file to replay. Leave its
                    # durable spool active so a planned backend restart can
                    # continue the same pull.
                    self._rebuild(force=True)
                    return
                self._emit_timeline(self.timeline_engine.interrupt('monitor_stopped'))
                self.callout_engine.reset()
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
                self.callout_engine.reset()
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
                received = datetime.now(timezone.utc)
                item['received_at'] = received.isoformat()
                event_epoch = (item.get('log_end_timestamp')
                               if item.get('type') == 'ENCOUNTER_ENDED'
                               else item.get('effective_log_timestamp'))
                if isinstance(event_epoch, (int, float)):
                    item['event_age_seconds'] = round(
                        max(0.0, received.timestamp() - float(event_epoch)), 3)
                self._updates.append(item)
                if str(item.get('type') or '').startswith('ENCOUNTER_'):
                    self._lifecycle_updates.append(item.copy())

    def _emit_callouts(self, events):
        """Publish bounded trusted speech events on the existing private update feed."""
        with self._lock:
            for event in events:
                item = dict(event)
                received = datetime.now(timezone.utc)
                item['received_at'] = received.isoformat()
                event_epoch = item.get('effective_log_timestamp')
                if isinstance(event_epoch, (int, float)):
                    raw_age = received.timestamp() - float(event_epoch)
                    if raw_age < -MAX_FUTURE_CALLOUT_SECONDS:
                        continue
                    item['event_age_seconds'] = round(
                        max(0.0, raw_age), 3)
                self._cursor += 1
                item['cursor'] = self._cursor
                self._updates.append(item)
                # Combat batches can evict thousands of ordinary updates before
                # Carter polls. Preserve each sparse callout like lifecycle events.
                self._lifecycle_updates.append(item.copy())

    @staticmethod
    def _open_spool_append(path):
        return path.open('ab', buffering=1024 * 1024)

    def _process_line(self, raw, *, spool_output=None):
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
                    self.callout_engine.reset()
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
            callout_profile = self.callout_engine.start(timeline_encounter)
            timeline_events = self.timeline_engine.start(timeline_encounter)
            if callout_profile:
                for item in timeline_events:
                    if item.get('type') == 'ENCOUNTER_STARTED':
                        item['callout_profile_id'] = callout_profile['profile_id']
                        item['callout_mechanic_count'] = len(callout_profile['mechanics'])
            self._emit_timeline(timeline_events)
            self._rebuild()
            return
        if self._active_spool:
            if spool_output is None:
                with self._open_spool_append(self._active_spool) as output:
                    output.write(raw)
            else:
                spool_output.write(raw)
            self._dirty = True
            self._append_update('combat_event', event)
            self._emit_callouts(self.callout_engine.process(event))
            if event.kind != 'ENCOUNTER_END':
                self._emit_timeline(self.timeline_engine.tick(event.timestamp))
            if event.kind == 'UNIT_DIED' and not self.remote:
                self._rebuild(force=True)
            if event.kind == 'ENCOUNTER_END':
                if (len(event.fields) < 6 or not self._active_ref or
                        number(event.fields[1]) != self._active_ref.encounter_id):
                    return
                success = len(event.fields) >= 6 and event.fields[5] == '1'
                self._emit_timeline(self.timeline_engine.end(success, event.timestamp))
                self.callout_engine.end()
                self._rebuild(force=True)
                with self._lock:
                    self._state = 'completed'
                    self._persist_snapshot('completed')
                    if self._timeline:
                        self._completed.append(self._timeline)
                    completed_spool = self._active_spool
                    self._active_spool = None
                    if completed_spool:
                        completed_spool.unlink(missing_ok=True)

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

    @staticmethod
    def _same_encounter(left, right):
        return (left.encounter_id == right.encounter_id
                and abs(float(left.start) - float(right.start)) < 0.001)

    def _recover_remote_spool(self):
        """Restore a remote pull whose backend process stopped mid-encounter."""
        spool = self.storage / f'{self.session_id}-active.log'
        if not spool.is_file():
            return
        try:
            refs = scan_encounters(spool)
            if not refs:
                return
            ref = refs[-1]
            if any(self._same_encounter(item['timeline'].encounter, ref)
                   for item in self._archived.values()):
                spool.unlink(missing_ok=True)
                return

            self._attempt += 1
            ref.attempt = self._attempt
            self._active_spool = spool
            self._active_ref = ref
            self._timeline = analyze_encounter(spool, ref)
            self._last_start_signature = (ref.encounter_id, ref.start)
            self._dirty = False
            self._last_rebuild = time.monotonic()

            encounter = TimelineEncounter(
                self.session_id, f'{self.session_id}-{self._attempt:04d}', self._attempt,
                ref.encounter_id, ref.name, ref.difficulty, ref.group_size, ref.start,
            )
            self.timeline_engine.start(encounter)
            self.callout_engine.start(encounter)
            self._last_event_epoch = ref.start
            with spool.open('rb') as source:
                for raw in source:
                    event = parse_event(decode_log_line(raw).rstrip('\r\n'))
                    if event.timestamp is None:
                        continue
                    self._last_event_epoch = event.timestamp
                    self._last_event_at = datetime.fromtimestamp(
                        event.timestamp, timezone.utc,
                    ).isoformat()
                    if event.kind not in {'ENCOUNTER_START', 'ENCOUNTER_END'}:
                        self.timeline_engine.tick(event.timestamp)
                        self.callout_engine.process(event, suppress_emission=True)

            if ref.end is None:
                self._state, self._error_code = 'active', None
                return

            self.timeline_engine.end(ref.success, ref.end)
            self.callout_engine.end()
            self._state, self._error_code = 'completed', None
            self._persist_snapshot('completed')
            self._completed.append(self._timeline)
            self._active_spool = None
            spool.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            self.callout_engine.reset()
            self._active_spool = None
            self._active_ref = None

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
            sender_diagnostics = (dict(self._sender_diagnostics)
                                  if self._sender_diagnostics else None)
            if sender_diagnostics:
                try:
                    received = datetime.fromisoformat(
                        sender_diagnostics['received_at'].replace('Z', '+00:00'))
                    sender_diagnostics['heartbeat_age_seconds'] = max(
                        0.0, (datetime.now(timezone.utc) - received).total_seconds())
                except (KeyError, TypeError, ValueError):
                    sender_diagnostics['heartbeat_age_seconds'] = None
            return {
                'schema_version': LIVE_SCHEMA_VERSION,
                'source_type': 'remote_live_log' if self.remote else 'local_live_log',
                'session_id': self.session_id, 'feed_id': self.feed_id,
                'source_name': self.source_name,
                'access_token_ready': True,
                'access_token_fingerprint': self.access_token_fingerprint,
                'configured': self.configured,
                'available': self.remote or bool(self.source and self.source.is_file()),
                'encounter_state': self._state, 'cursor': self._cursor,
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'last_event_at': self._last_event_at, 'parse_errors': self._parse_errors,
                'fight_time_seconds': duration, 'complete': self._state == 'completed',
                'stale': bool(self._error_code), 'error_code': self._error_code,
                'sender_diagnostics': sender_diagnostics,
                'live_callouts': self.callout_engine.status(),
                'mechanic_timeline': {
                    key: value for key, value in self.timeline_snapshot().items()
                    if key != 'events'
                },
                'timer_module': self.timer_module_status(),
            }

    def updates(self, since=0, limit=100):
        with self._lock:
            history = list(self._updates)
            oldest = history[0]['cursor'] if history else self._cursor + 1
            protected = [item.copy() for item in self._lifecycle_updates
                         if item['cursor'] > int(since) and item['cursor'] < oldest]
            if protected:
                # Deliver evicted lifecycle and guide-callout events before reporting the gap.
                # The caller will advance past them, then receive CURSOR_EXPIRED
                # and resume from the ordinary retained history.
                return protected[:limit], protected[0]['cursor']
            if history and int(since) < history[0]['cursor'] - 1:
                return None, history[0]['cursor']
            matches = [item.copy() for item in history if item['cursor'] > int(since)]
            return matches[:limit], oldest
