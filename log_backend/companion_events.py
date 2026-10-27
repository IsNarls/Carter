"""Bounded, guild-private companion events, independent of bulk-log processing."""

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time

if __package__:
    from .live_callouts import CalloutProfileRegistry, DEFAULT_PROFILE_DIRECTORY, PROFILE_ID, MECHANIC_ID
else:
    from live_callouts import CalloutProfileRegistry, DEFAULT_PROFILE_DIRECTORY, PROFILE_ID, MECHANIC_ID


MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENTS = 100
MAX_RECEIPTS = 4096
MAX_ATTEMPTS = 128
MAX_SESSIONS = 32
MAX_HISTORY = 1000
MAX_RETENTION_SECONDS = 86400
MAX_INTEGER = 2 ** 53 - 1
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$')
COMMON = {'event_id', 'session_id', 'attempt_id', 'sequence', 'type',
          'encounter_id', 'difficulty_id', 'log_timestamp', 'detected_at'}
EXTRA = {'ENCOUNTER_STARTED': set(), 'ENCOUNTER_ENDED': {'result'},
         'MECHANIC_OBSERVED': {'profile_id', 'profile_revision', 'mechanic_id', 'occurrence'}}


def timestamp(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError('Timestamps must be UTC RFC3339 with at most six fractional digits.')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def validate_envelope(body):
    if (not isinstance(body, dict) or set(body) != {'schema_version', 'events'}
            or type(body['schema_version']) is not int or body['schema_version'] != 1
            or not isinstance(body['events'], list) or not 1 <= len(body['events']) <= MAX_EVENTS):
        raise ValueError('Expected schema_version 1 and 1 through 100 events.')
    for event in body['events']:
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise ValueError('Every event must be an object with a supported type.')
        extra = EXTRA.get(event['type'])
        if extra is None or set(event) != COMMON | extra:
            raise ValueError('Event fields must exactly match its event type.')
        for key in ('event_id', 'session_id', 'attempt_id'):
            if not isinstance(event[key], str) or not UUID.fullmatch(event[key]):
                raise ValueError('Identifiers must be lowercase, hyphenated UUIDs.')
        for key in ('sequence', 'encounter_id', 'difficulty_id', 'profile_revision', 'occurrence'):
            if key in event and (type(event[key]) is not int or not 1 <= event[key] <= MAX_INTEGER):
                raise ValueError('Integer fields must be positive safe JSON integers.')
        timestamp(event['log_timestamp'])
        timestamp(event['detected_at'])
        if event['type'] == 'ENCOUNTER_ENDED' and event['result'] not in ('KILL', 'WIPE'):
            raise ValueError('Encounter result must be KILL or WIPE.')
        if event['type'] == 'MECHANIC_OBSERVED':
            for key, pattern in (('profile_id', PROFILE_ID), ('mechanic_id', MECHANIC_ID)):
                if not isinstance(event[key], str) or not pattern.fullmatch(event[key]):
                    raise ValueError('Profile and mechanic identifiers are invalid.')
    return body['events']


class CompanionEvents:
    """One guild's durable retry ledger and ephemeral announcement feed."""

    def __init__(self, storage, *, clock=time.time):
        self.storage = Path(storage) / 'live-analysis'
        self.path = self.storage / 'companion-state.json'
        self.clock = clock
        self.lock = threading.RLock()
        self.registry = CalloutProfileRegistry((DEFAULT_PROFILE_DIRECTORY,
                                               Path(storage) / 'callout-profiles'))
        self.source = 'raw_log'
        self.feed_id = secrets.token_hex(16)
        self.cursor = 0
        self.history = deque(maxlen=MAX_HISTORY)
        self.receipts = OrderedDict()
        self.attempts = OrderedDict()
        self.sessions = OrderedDict()
        self.telemetry = {'received_events': 0, 'accepted': 0, 'duplicate': 0,
                          'expired': 0, 'rejected': 0}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        # Fail closed on corrupt state rather than replaying previously spoken events.
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Companion state exceeds its size limit.')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('source') not in ('raw_log', 'companion'):
            raise ValueError('Companion state is invalid.')
        self.source = data['source']
        self.receipts = OrderedDict(data['receipts'][-MAX_RECEIPTS:])
        self.attempts = OrderedDict(data['attempts'][-MAX_ATTEMPTS:])
        self.sessions = OrderedDict(data['sessions'][-MAX_SESSIONS:])
        self._prune()

    def _prune(self):
        cutoff = self.clock() - MAX_RETENTION_SECONDS
        for values, bound in ((self.receipts, MAX_RECEIPTS), (self.attempts, MAX_ATTEMPTS),
                              (self.sessions, MAX_SESSIONS)):
            for key in list(values):
                if values[key]['updated_at'] < cutoff:
                    del values[key]
            while len(values) > bound:
                values.popitem(last=False)

    def _save(self):
        self._prune()
        self.storage.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.part')
        temporary.write_text(json.dumps({
            'version': 1, 'source': self.source, 'receipts': list(self.receipts.items()),
            'attempts': list(self.attempts.items()), 'sessions': list(self.sessions.items()),
        }, separators=(',', ':')), encoding='utf-8')
        temporary.replace(self.path)

    def set_source(self, source):
        if source not in ('raw_log', 'companion'):
            raise ValueError('Source must be raw_log or companion.')
        with self.lock:
            if source != self.source:
                previous = self.source
                self.source = source
                try:
                    self._save()
                except Exception:
                    self.source = previous
                    raise
                self.feed_id = secrets.token_hex(16)
                self.history.clear()
                self.cursor = 0
            return self.status()

    def status(self):
        with self.lock:
            return {'schema_version': 1, 'announcement_source': self.source,
                    'session_id': 'companion', 'feed_id': self.feed_id,
                    'cursor': self.cursor, 'configured': True, 'available': True,
                    'source_type': 'companion', 'telemetry': dict(self.telemetry)}

    def receive(self, body):
        events = validate_envelope(body)
        started = time.monotonic()
        with self.lock:
            self._prune()
            self.registry.reload()
            checkpoint = deepcopy((self.receipts, self.attempts, self.sessions, self.telemetry))
            results, pending = [], []
            for event in events:
                event_id = event['event_id']
                digest = hashlib.sha256(json.dumps(event, sort_keys=True,
                                                  separators=(',', ':')).encode()).hexdigest()
                old = self.receipts.get(event_id)
                if old:
                    status, reason, update = ('duplicate', None, None) if old['digest'] == digest else (
                        'rejected', 'event_id_conflict', None)
                else:
                    status, reason, update = self._accept(event)
                    self.receipts[event_id] = {'digest': digest, 'updated_at': self.clock()}
                result = {'event_id': event_id, 'status': status}
                if reason:
                    result['reason'] = reason
                results.append(result)
                self.telemetry['received_events'] += 1
                self.telemetry[status] += 1
                if update:
                    pending.append(update)
            # Persist retry/lifecycle state before making events visible to Carter.
            try:
                self._save()
            except Exception:
                self.receipts, self.attempts, self.sessions, self.telemetry = checkpoint
                raise
            for update in pending:
                self.cursor += 1
                update['cursor'] = self.cursor
                self.history.append(update)
            self.telemetry['last_request_ms'] = round((time.monotonic() - started) * 1000, 3)
            self.telemetry['last_received_at'] = datetime.fromtimestamp(
                self.clock(), timezone.utc).isoformat()
            return {'schema_version': 1, 'results': results}

    def _accept(self, event):
        now = self.clock()
        log_at, detected_at = timestamp(event['log_timestamp']), timestamp(event['detected_at'])
        if log_at > now + 2 or detected_at > now + 2 or detected_at < log_at:
            return 'rejected', 'invalid_event_time', None
        if now - log_at > MAX_RETENTION_SECONDS:
            return 'expired', 'outside_retention', None
        if self.source != 'companion':
            return 'rejected', 'source_disabled', None
        session, attempt_id, kind = event['session_id'], event['attempt_id'], event['type']
        key = f'{session}/{attempt_id}'
        attempt = self.attempts.get(key)
        session_state = self.sessions.get(session)
        high = session_state['sequence'] if session_state else 0
        if attempt and (attempt['encounter_id'] != event['encounter_id']
                        or attempt['difficulty_id'] != event['difficulty_id']):
            return 'rejected', 'attempt_mismatch', None
        if attempt and attempt['ended']:
            return ('duplicate', None, None) if kind == 'ENCOUNTER_ENDED' else (
                'rejected', 'attempt_ended', None)
        # A late end is authoritative even if a later sequence reached us first.
        if event['sequence'] <= high and kind != 'ENCOUNTER_ENDED':
            return 'rejected', 'out_of_order', None
        profile = self.registry.select(event['encounter_id'], event['difficulty_id'], reload=False)
        mechanic = None
        if kind == 'MECHANIC_OBSERVED':
            if (profile is None or event['profile_id'] != profile['profile_id']
                    or event['profile_revision'] != profile.get('profile_revision', 1)):
                return 'rejected', 'profile_mismatch', None
            mechanic = next((m for m in profile['mechanics']
                             if m['mechanic_id'] == event['mechanic_id']), None)
            if mechanic is None:
                return 'rejected', 'unknown_mechanic', None
            if attempt is None:
                return 'rejected', 'missing_attempt', None
            occurrence = attempt['occurrences'].get(event['mechanic_id'], 0)
            if event['occurrence'] <= occurrence:
                return 'duplicate', None, None
            if log_at < attempt['started_at']:
                return 'rejected', 'before_attempt_start', None
        if kind == 'ENCOUNTER_STARTED':
            if attempt:
                return 'duplicate', None, None
            # A new pull supersedes previous active pulls, even across companion restarts.
            for previous in self.attempts.values():
                previous['ended'] = True
        if attempt is None:
            attempt = {'encounter_id': event['encounter_id'], 'difficulty_id': event['difficulty_id'],
                       'started_at': log_at, 'ended': False, 'occurrences': {}, 'updated_at': now}
            self.attempts[key] = attempt
        if kind == 'ENCOUNTER_ENDED':
            if log_at < attempt['started_at']:
                return 'rejected', 'before_attempt_start', None
            attempt['ended'] = True
        if mechanic:
            attempt['occurrences'][event['mechanic_id']] = event['occurrence']
        attempt['updated_at'] = now
        self.attempts.move_to_end(key)
        self.sessions[session] = {'sequence': max(high, event['sequence']), 'updated_at': now}
        self.sessions.move_to_end(session)
        age = max(0, now - log_at)
        self.telemetry['last_event_age_seconds'] = round(age, 3)
        self.telemetry['last_detection_delay_seconds'] = round(detected_at - log_at, 3)
        self.telemetry['last_transport_delay_seconds'] = round(max(0, now - detected_at), 3)
        max_age = mechanic['max_lateness_seconds'] if mechanic else 15
        update = {'type': 'MECHANIC_CALLOUT' if mechanic else kind,
                  'session_id': session, 'attempt_id': attempt_id,
                  'encounter_id': event['encounter_id'], 'difficulty': event['difficulty_id'],
                  'encounter_name': profile['encounter_name'] if profile else f"encounter {event['encounter_id']}",
                  'effective_log_timestamp': log_at, 'event_age_seconds': round(age, 3),
                  'received_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  'announcement_source': 'companion'}
        if mechanic:
            update.update({k: mechanic[k] for k in ('mechanic_id', 'audio_key', 'priority',
                                                   'max_lateness_seconds', 'confidence', 'wording_status')})
            update.update(message=mechanic['text'], profile_id=profile['profile_id'],
                          occurrence=event['occurrence'])
        elif kind == 'ENCOUNTER_ENDED':
            update.update(result=event['result'], log_end_timestamp=log_at)
        if age > max_age:
            # An expired end still cancels Carter's queued guidance; its raw timestamp
            # ensures the ordinary speech path suppresses the obsolete result.
            return 'expired', None, update if kind == 'ENCOUNTER_ENDED' else None
        return 'accepted', None, update

    def updates(self, since=0, limit=100):
        since, limit = max(0, int(since)), min(500, max(1, int(limit)))
        with self.lock:
            oldest = self.history[0]['cursor'] if self.history else self.cursor + 1
            if since < oldest - 1:
                return None, oldest
            items = []
            for item in self.history:
                if item['cursor'] <= since:
                    continue
                # Preserve cursor progression while suppressing tactics for closed pulls.
                item = dict(item)
                key = f"{item['session_id']}/{item['attempt_id']}"
                attempt = self.attempts.get(key)
                if item['type'] in ('MECHANIC_CALLOUT', 'ENCOUNTER_STARTED') and (not attempt or attempt['ended']):
                    item['type'] = 'COMPANION_MECHANIC_CANCELED'
                items.append(item)
                if len(items) >= limit:
                    break
            return items, oldest
