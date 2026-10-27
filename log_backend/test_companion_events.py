"""Companion contract, durable lifecycle, isolation and independent delivery tests."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from log_backend.analysis_api import create_server
from log_backend.companion_events import CompanionEvents, MAX_EVENTS, MAX_RECEIPTS
from log_backend.live_log_service import LiveLogService, ensure_live_access_token


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.now = time.time()
        self.service = CompanionEvents(self.root, clock=lambda: self.now)
        self.service.set_source('companion')
        self.session, self.attempt = str(uuid4()), str(uuid4())

    def tearDown(self):
        self.directory.cleanup()

    def event(self, kind='ENCOUNTER_STARTED', sequence=1, **changes):
        event = dict(event_id=str(uuid4()), session_id=self.session, attempt_id=self.attempt,
                     sequence=sequence, type=kind, encounter_id=3470, difficulty_id=17,
                     log_timestamp=utc(self.now - .2), detected_at=utc(self.now - .1))
        if kind == 'MECHANIC_OBSERVED':
            event.update(profile_id='nekzali-observed-v1', profile_revision=1,
                         mechanic_id='hungering_pyre', occurrence=1)
        if kind == 'ENCOUNTER_ENDED':
            event['result'] = 'KILL'
        event.update(changes)
        return event

    def send(self, *events):
        return self.service.receive({'schema_version': 1, 'events': list(events)})['results']

    def test_trusted_mechanic_text_and_generic_lifecycle(self):
        self.assertEqual(self.send(self.event())[0]['status'], 'accepted')
        result = self.send(self.event('MECHANIC_OBSERVED', 2))
        self.assertEqual(result[0]['status'], 'accepted')
        updates, _ = self.service.updates()
        self.assertEqual(updates[-1]['message'], 'Group soak.')
        self.assertEqual(updates[-1]['type'], 'MECHANIC_CALLOUT')
        self.assertEqual(updates[-1]['max_lateness_seconds'], 5)
        self.assertAlmostEqual(updates[-1]['effective_log_timestamp'], self.now - .2, places=5)
        self.assertIn('last_transport_delay_seconds', self.service.status()['telemetry'])

    def test_unknown_boss_start_and_wipe_end(self):
        self.send(self.event(encounter_id=999999))
        self.send(self.event('ENCOUNTER_ENDED', 2, encounter_id=999999, result='WIPE'))
        updates, _ = self.service.updates()
        self.assertEqual(updates[-1]['result'], 'WIPE')
        self.assertEqual(updates[-1]['encounter_name'], 'encounter 999999')

    def test_profile_difficulty_revision_and_mechanic_match(self):
        self.send(self.event())
        for changes in ({'profile_id': 'wrong'}, {'profile_revision': 2},
                        {'difficulty_id': 16}, {'mechanic_id': 'invented'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 2, **changes))[0]['status'],
                                 'rejected')

    def test_retry_conflict_and_semantic_duplicate(self):
        start = self.event()
        self.send(start)
        self.assertEqual(self.send(start)[0]['status'], 'duplicate')
        self.assertEqual(self.send(dict(start, encounter_id=2))[0]['reason'], 'event_id_conflict')
        mechanic = self.event('MECHANIC_OBSERVED', 2)
        self.send(mechanic)
        self.assertEqual(self.send(dict(mechanic, event_id=str(uuid4()), sequence=3))[0]['status'],
                         'duplicate')
        self.assertEqual(len(self.service.updates()[0]), 2)

    def test_restart_retains_dedup_source_and_closed_attempt(self):
        start = self.event()
        end = self.event('ENCOUNTER_ENDED', 3)
        self.send(start, end)
        self.service = CompanionEvents(self.root, clock=lambda: self.now)
        self.assertEqual(self.service.source, 'companion')
        self.assertEqual(self.send(start, end)[0]['status'], 'duplicate')
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 4))[0]['reason'], 'attempt_ended')
        self.assertEqual(self.service.updates()[0], [])

    def test_end_is_authoritative_even_out_of_order_and_without_start(self):
        self.send(self.event(), self.event('MECHANIC_OBSERVED', 5))
        self.assertEqual(self.send(self.event('ENCOUNTER_ENDED', 3))[0]['status'], 'accepted')
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 6))[0]['reason'], 'attempt_ended')
        self.assertNotIn('MECHANIC_CALLOUT', [e['type'] for e in self.service.updates()[0]])
        other = str(uuid4())
        self.send(self.event('ENCOUNTER_ENDED', 7, attempt_id=other))
        self.assertEqual(self.send(self.event(sequence=8, attempt_id=other))[0]['reason'], 'attempt_ended')

    def test_missing_start_out_of_order_and_attempt_mismatch(self):
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 2))[0]['reason'], 'missing_attempt')
        self.send(self.event())
        self.send(self.event('MECHANIC_OBSERVED', 4))
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 3, occurrence=2))[0]['reason'], 'out_of_order')
        self.assertEqual(self.send(self.event('ENCOUNTER_ENDED', 5, encounter_id=99))[0]['reason'], 'attempt_mismatch')

    def test_expired_start_establishes_attempt_and_expired_end_closes_it(self):
        self.assertEqual(self.send(self.event(log_timestamp=utc(self.now - 60)))[0]['status'], 'expired')
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 2))[0]['status'], 'accepted')
        result = self.send(self.event('ENCOUNTER_ENDED', 3, log_timestamp=utc(self.now - 20)))
        self.assertEqual(result[0]['status'], 'expired')
        updates, _ = self.service.updates()
        self.assertEqual(updates[-1]['type'], 'ENCOUNTER_ENDED')
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 4))[0]['reason'], 'attempt_ended')

    def test_expired_mechanic_and_future_or_inconsistent_times(self):
        self.send(self.event(log_timestamp=utc(self.now - 30)))
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 2, log_timestamp=utc(self.now - 6)))[0]['status'], 'expired')
        for changes in ({'log_timestamp': utc(self.now + 10)},
                        {'detected_at': utc(self.now + 10)},
                        {'detected_at': utc(self.now - 10)}):
            self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 3, **changes))[0]['reason'],
                             'invalid_event_time')

    def test_source_switch_clears_speech_and_blocks_inactive_source(self):
        self.send(self.event())
        previous = self.service.feed_id
        self.service.set_source('raw_log')
        self.assertNotEqual(self.service.feed_id, previous)
        self.assertEqual(self.service.updates()[0], [])
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 2))[0]['reason'], 'source_disabled')

    def test_new_pull_supersedes_previous_pull(self):
        self.send(self.event())
        self.send(self.event(sequence=2, attempt_id=str(uuid4())))
        self.assertEqual(self.send(self.event('MECHANIC_OBSERVED', 3))[0]['reason'], 'attempt_ended')

    def test_invalid_event_shapes_fail_whole_envelope(self):
        for event in (dict(self.event(), message='speak client text'),
                      self.event(sequence=True), self.event(sequence=2**53),
                      self.event(event_id='not-a-uuid'), self.event(detected_at='yesterday'),
                      self.event(type='ARBITRARY_SPEECH')):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.send(event)
        self.assertEqual(self.service.cursor, 0)
        with self.assertRaises(ValueError):
            self.service.receive({'schema_version': True, 'events': [self.event()]})
        with self.assertRaises(ValueError):
            self.send(*[self.event() for _ in range(MAX_EVENTS + 1)])

    def test_state_failure_does_not_ack_or_poison_retries(self):
        event = self.event()
        with patch.object(self.service, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.send(event)
        self.assertEqual(self.service.cursor, 0)
        self.assertEqual(self.send(event)[0]['status'], 'accepted')

    def test_dedup_history_and_attempt_storage_are_bounded(self):
        with patch('log_backend.companion_events.MAX_RECEIPTS', 8), \
             patch('log_backend.companion_events.MAX_ATTEMPTS', 4), \
             patch('log_backend.companion_events.MAX_SESSIONS', 2):
            for seq in range(1, 20):
                self.send(self.event(sequence=seq, attempt_id=str(uuid4()), session_id=str(uuid4())))
            self.assertLessEqual(len(self.service.receipts), 8)
            self.assertLessEqual(len(self.service.attempts), 4)
            self.assertLessEqual(len(self.service.sessions), 2)


class EventsHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.guild = '123456789'
        self.guild_root = self.root / 'guilds' / self.guild
        self.token = ensure_live_access_token(self.guild_root)['path'].read_text().strip()
        self.server = create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/api/v1'
        self.events = self.server.registry.companion(self.guild)
        self.events.set_source('companion')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, body=None, *, guild=None, token=None, raw=None, content_type='application/json'):
        headers = {'X-Carter-Guild-ID': guild or self.guild,
                   'X-Carter-Live-Token': self.token if token is None else token,
                   'Content-Type': content_type}
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        with urlopen(Request(self.url + path, data=data, headers=headers), timeout=3) as response:
            return json.load(response)

    def envelope(self):
        now = utc(time.time() - .1)
        return {'schema_version': 1, 'events': [dict(event_id=str(uuid4()), session_id=str(uuid4()),
            attempt_id=str(uuid4()), sequence=1, type='ENCOUNTER_STARTED', encounter_id=3470,
            difficulty_id=17, log_timestamp=now, detected_at=now)]}

    def test_authentication_is_guild_scoped_and_does_not_provision(self):
        for changes in ({'token': 'x' * 43}, {'guild': '987654321'}, {'guild': '../escape'}):
            with self.subTest(changes=changes), self.assertRaises(HTTPError) as raised:
                self.request('/live/events', self.envelope(), **changes)
            self.assertIn(raised.exception.code, (400, 401))
        self.assertFalse((self.root / 'guilds' / '987654321').exists())
        self.assertEqual(self.request('/live/events', self.envelope())['results'][0]['status'], 'accepted')
        self.assertNotIn(self.token, json.dumps(self.request('/live/announcements/status')))

    def test_strict_json_limits_and_unsupported_fields(self):
        for raw, content_type, expected in ((b'{}', 'text/plain', 415),
                (b'x' * 65537, 'application/json', 413),
                (b'{"events":[],"events":[]}', 'application/json', 400),
                (b'{"events":NaN}', 'application/json', 400)):
            with self.subTest(expected=expected), self.assertRaises(HTTPError) as raised:
                self.request('/live/events', raw=raw, content_type=content_type)
            self.assertEqual(raised.exception.code, expected)
        event = self.envelope()
        event['events'][0]['speech'] = 'client speech'
        with self.assertRaises(HTTPError) as raised:
            self.request('/live/events', event)
        self.assertEqual(raised.exception.code, 400)

    def test_companion_events_and_polling_bypass_bulk_locks(self):
        bulk = LiveLogService(None, self.guild_root, remote=True)
        self.server.registry.live = bulk
        self.server.registry.live_guild_id = self.guild
        # Simulate a long-running full-log ingest/final analysis holding both locks.
        with bulk._lock, self.server.registry._lock:
            result = self.request('/live/events', self.envelope())
            self.assertEqual(result['results'][0]['status'], 'accepted')
            feed = self.request('/live/announcements?since=0')
            self.assertEqual(feed['updates'][0]['type'], 'ENCOUNTER_STARTED')
        bulk.stop()

    def test_source_persists_and_raw_log_analysis_routes_stay_separate(self):
        self.request('/live/events', self.envelope())
        status = self.request('/live/announcement-source')
        self.assertEqual(status['announcement_source'], 'companion')
        self.request('/live/announcement-source', {'source': 'raw_log'})
        self.assertEqual(CompanionEvents(self.guild_root).source, 'raw_log')
        self.assertEqual(self.request('/live/events', self.envelope())['results'][0]['reason'], 'source_disabled')
        self.request('/live/announcement-source', {'source': 'companion'})
        self.assertEqual(self.request('/live/announcements')['updates'], [])

    def test_two_guilds_never_share_events(self):
        other = '987654321'
        other_token = ensure_live_access_token(self.root / 'guilds' / other)['path'].read_text().strip()
        self.request('/live/events', self.envelope())
        self.request('/live/announcement-source', {'source': 'companion'}, guild=other, token=other_token)
        self.assertEqual(self.request('/live/announcements', guild=other, token=other_token)['updates'], [])
        self.assertEqual(len(self.request('/live/announcements')['updates']), 1)

    def test_selected_feed_cannot_mix_raw_and_companion_events(self):
        bulk = LiveLogService(None, self.guild_root, remote=True)
        self.server.registry.live = bulk
        self.server.registry.live_guild_id = self.guild
        # The raw path can keep generating lifecycle data while the companion is selected.
        bulk._emit_test_announcement('a' * 32)
        self.request('/live/events', self.envelope())
        feed = self.request('/live/announcements')
        self.assertEqual([e['type'] for e in feed['updates']], ['ENCOUNTER_STARTED'])
        self.request('/live/announcement-source', {'source': 'raw_log'})
        raw = self.request('/live/announcements')
        self.assertEqual([e['type'] for e in raw['updates']], ['ANNOUNCEMENT_TEST_REQUESTED'])
        self.assertNotEqual(feed['feed_id'], raw['feed_id'])
        bulk.stop()


if __name__ == '__main__':
    unittest.main()
