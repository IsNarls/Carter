import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

if __package__:
    from .analysis_api import GuildAnalysisRegistry
    from .combat_log_core import Event
    from .live_callouts import (CalloutProfileRegistry, ObservedCalloutEngine,
                                PROFILE_FORMAT, validate_callout_profile)
    from .live_log_service import MAX_UPDATE_HISTORY, LiveLogService
    from .mechanic_timeline import TimelineEncounter
else:
    from analysis_api import GuildAnalysisRegistry
    from combat_log_core import Event
    from live_callouts import (CalloutProfileRegistry, ObservedCalloutEngine,
                               PROFILE_FORMAT, validate_callout_profile)
    from live_log_service import MAX_UPDATE_HISTORY, LiveLogService
    from mechanic_timeline import TimelineEncounter


SOURCE_GUID = 'Creature-0-3883-3004-181484-259927-00005CDD13'
OTHER_GUID = 'Creature-0-3883-3004-181484-111111-00005CDD13'


def profile(*, text='Move now.', encounter_id=7000, difficulty=15):
    return {
        'format': PROFILE_FORMAT,
        'schema_version': 1,
        'profile_id': 'test-profile-v1',
        'encounter_id': encounter_id,
        'encounter_name': 'Test Guide Boss',
        'difficulty_ids': [difficulty],
        'boss_creature_ids': [259927],
        'predictions_enabled': False,
        'mechanics': [{
            'mechanic_id': 'move',
            'name': 'Move',
            'event_type': 'SPELL_CAST_START',
            'spell_id': 12345,
            'source_policy': {
                'kind': 'creature_allowlist',
                'creature_ids': [259927],
            },
            'grouping': {
                'mode': 'leading_edge_batch_window',
                'window_seconds': 0.25,
            },
            'duplicate_suppression_seconds': 0.0,
            'text': text,
            'audio_key': 'test_move',
            'priority': 90,
            'max_lateness_seconds': 5.0,
            'confidence': 0.95,
            'wording_status': 'provisional',
        }],
    }


def encounter(encounter_id=7000, difficulty=15, attempt=1):
    return TimelineEncounter(
        'session', f'attempt-{attempt}', attempt, encounter_id,
        'Test Guide Boss', difficulty, 5, 1000.0,
    )


def spell_event(timestamp, *, source=SOURCE_GUID, spell_id=12345, target='Player-1'):
    fields = (
        'SPELL_CAST_START', source, 'Test Guide Boss', '0', '0', target, 'Hero',
        '0', '0', str(spell_id), 'Move', '1',
    )
    return Event(timestamp, '', 'SPELL_CAST_START', 'Test Guide Boss', 'Hero',
                 'Move', '', fields)


def log_line(second, fields):
    output = io.StringIO()
    csv.writer(output, lineterminator='').writerow(fields)
    return f'10/26/2026 10:00:{second}-7  {output.getvalue()}\n'


def start_line(encounter_id=7000, difficulty=15):
    return log_line('00.000', [
        'ENCOUNTER_START', encounter_id, 'Test Guide Boss', difficulty, 5, 3004,
    ])


def cast_line(second='01.000', *, source=SOURCE_GUID, spell_id=12345):
    return log_line(second, [
        'SPELL_CAST_START', source, 'Test Guide Boss', 0, 0, 'Player-1', 'Hero',
        0, 0, spell_id, 'Move', 1,
    ])


def end_line(encounter_id=7000, difficulty=15, success=1):
    return log_line('03.000', [
        'ENCOUNTER_END', encounter_id, 'Test Guide Boss', difficulty, 5, success, 3000,
    ])


class LiveCalloutProfileTests(unittest.TestCase):
    def test_packaged_profiles_are_preloaded_and_exactly_scoped(self):
        registry = CalloutProfileRegistry()

        self.assertEqual(registry.status()['loaded_profiles'], 2)
        self.assertEqual(registry.select(3470, 15)['profile_id'], 'nekzali-observed-v1')
        self.assertEqual(registry.select(3470, 17)['profile_id'], 'nekzali-observed-v1')
        self.assertEqual(
            registry.select(3445, 15)['profile_id'], 'entombed-sentinels-observed-v1')
        self.assertIsNone(registry.select(3470, 16))
        self.assertIsNone(registry.select(9999, 15))
        self.assertEqual(registry.status()['errors'], [])

    def test_packaged_nekzali_profile_emits_for_observed_lfr_difficulty(self):
        engine = ObservedCalloutEngine(CalloutProfileRegistry())
        selected = engine.start(encounter(encounter_id=3470, difficulty=17))

        callouts = engine.process(spell_event(1001.0, spell_id=1284103))

        self.assertEqual(selected['profile_id'], 'nekzali-observed-v1')
        self.assertEqual(callouts[0]['mechanic_id'], 'possession_barrage')
        self.assertEqual(callouts[0]['message'], 'Barrage. Avoid the tank line.')

    def test_validation_rejects_unbounded_or_predictive_speech(self):
        invalid = profile(text='x' * 241)
        with self.assertRaisesRegex(ValueError, '240'):
            validate_callout_profile(invalid)

        invalid = profile()
        invalid['predictions_enabled'] = True
        with self.assertRaisesRegex(ValueError, 'observed'):
            validate_callout_profile(invalid)

    def test_engine_matches_source_groups_targets_and_counts_occurrences(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            path = Path(folder) / 'test.callout.json'
            path.write_text(json.dumps(profile()), encoding='utf-8')
            engine = ObservedCalloutEngine(CalloutProfileRegistry([path]))

            self.assertIsNotNone(engine.start(encounter()))
            self.assertEqual(engine.process(spell_event(1001.0, source=OTHER_GUID)), [])
            first = engine.process(spell_event(1002.0))
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]['message'], 'Move now.')
            self.assertEqual(first[0]['occurrence'], 1)
            self.assertEqual(first[0]['effective_log_timestamp'], 1002.0)
            self.assertEqual(first[0]['max_lateness_seconds'], 5.0)

            self.assertEqual(engine.process(spell_event(1002.0)), [])
            self.assertEqual(engine.process(spell_event(1002.1, target='Player-2')), [])
            second = engine.process(spell_event(1002.3, target='Player-3'))
            self.assertEqual(second[0]['occurrence'], 2)

            engine.end()
            self.assertEqual(engine.process(spell_event(1003.0)), [])

    def test_catalog_hot_loads_new_profile_on_the_next_pull(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            registry = CalloutProfileRegistry([root])
            engine = ObservedCalloutEngine(registry)
            self.assertIsNone(engine.start(encounter()))

            (root / 'new.callout.json').write_text(
                json.dumps(profile(text='New guide loaded.')), encoding='utf-8')
            selected = engine.start(encounter(attempt=2))

            self.assertEqual(selected['profile_id'], 'test-profile-v1')
            self.assertEqual(engine.process(spell_event(1001.0))[0]['message'],
                             'New guide loaded.')

    def test_later_guild_source_overrides_packaged_profile(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            global_root = root / 'global'
            guild_root = root / 'guild'
            global_root.mkdir()
            guild_root.mkdir()
            (global_root / 'boss.callout.json').write_text(
                json.dumps(profile(text='Global text.')), encoding='utf-8')
            (guild_root / 'boss.callout.json').write_text(
                json.dumps(profile(text='Guild text.')), encoding='utf-8')

            registry = CalloutProfileRegistry([global_root, guild_root])

            self.assertEqual(registry.select(7000, 15)['mechanics'][0]['text'],
                             'Guild text.')
            self.assertEqual(registry.status()['errors'], [])


class LiveCalloutServiceTests(unittest.TestCase):
    def make_registry(self, root):
        profile_path = root / 'test.callout.json'
        profile_path.write_text(json.dumps(profile()), encoding='utf-8')
        return CalloutProfileRegistry([profile_path])

    def test_managed_windows_ingest_services_receive_the_packaged_catalog(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            registry = GuildAnalysisRegistry(Path(folder))
            try:
                service, _jobs = registry.scope('123456789', require_live=True)

                self.assertEqual(service.live.status()['source_type'], 'remote_live_log')
                self.assertEqual(service.live.status()['live_callouts']['loaded_profiles'], 2)
            finally:
                registry.close()

    def test_remote_live_service_emits_known_callout_and_unknown_fallback_lifecycle(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            known = LiveLogService(
                None, root / 'known', remote=True,
                callout_registry=self.make_registry(root),
            )
            try:
                payload = (start_line() + cast_line() + end_line()).encode()
                known.ingest_remote_batch('known-stream-001', 0, payload)
                updates, _oldest = known.updates(0, 100)
                callouts = [item for item in updates
                            if item['type'] == 'MECHANIC_CALLOUT']
                self.assertEqual(len(callouts), 1)
                self.assertEqual(callouts[0]['message'], 'Move now.')
                self.assertEqual(callouts[0]['occurrence'], 1)
                started = next(item for item in updates
                               if item['type'] == 'ENCOUNTER_STARTED')
                self.assertEqual(started['callout_profile_id'], 'test-profile-v1')
                self.assertEqual(known.status()['live_callouts']['loaded_profiles'], 1)
                self.assertIsNone(known.status()['live_callouts']['active_profile_id'])
            finally:
                known.stop()

            unknown = LiveLogService(
                None, root / 'unknown', remote=True,
                callout_registry=self.make_registry(root),
            )
            try:
                payload = (start_line(8000) + cast_line() + end_line(8000)).encode()
                unknown.ingest_remote_batch('unknown-stream-001', 0, payload)
                updates, _oldest = unknown.updates(0, 100)
                self.assertFalse(any(item['type'] == 'MECHANIC_CALLOUT'
                                     for item in updates))
                self.assertEqual(
                    [item['type'] for item in updates if item['type'].startswith('ENCOUNTER_')],
                    ['ENCOUNTER_STARTED', 'ENCOUNTER_ENDED'],
                )
            finally:
                unknown.stop()

    def test_callout_survives_update_churn(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            live = LiveLogService(
                None, root / 'storage', remote=True,
                callout_registry=self.make_registry(root),
            )
            try:
                opening = (start_line() + cast_line()).encode()
                live.ingest_remote_batch('churn-stream-001', 0, opening)
                callout = next(item for item in live.updates(0, 100)[0]
                               if item['type'] == 'MECHANIC_CALLOUT')
                for _index in range(MAX_UPDATE_HISTORY + 5):
                    live._append_update('combat_event')

                protected, _oldest = live.updates(callout['cursor'] - 1, 100)

                self.assertEqual(protected[0]['type'], 'MECHANIC_CALLOUT')
                self.assertEqual(protected[0]['cursor'], callout['cursor'])
            finally:
                live.stop()

    def test_active_pull_recovery_rebuilds_grouping_without_replaying_speech(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            registry = self.make_registry(root)
            storage = root / 'storage'
            first = LiveLogService(
                None, storage, remote=True, callout_registry=registry,
            )
            opening = (start_line() + cast_line('01.000')).encode()
            first.ingest_remote_batch('restart-stream-001', 0, opening)
            first.stop()

            resumed = LiveLogService(
                None, storage, remote=True, callout_registry=registry,
            )
            try:
                continuation = (cast_line('01.100') + cast_line('01.300')).encode()
                resumed.ingest_remote_batch(
                    'restart-stream-001', len(opening), continuation)
                updates, _oldest = resumed.updates(0, 100)
                callouts = [item for item in updates
                            if item['type'] == 'MECHANIC_CALLOUT']

                self.assertEqual(len(callouts), 1)
                self.assertEqual(callouts[0]['occurrence'], 2)
            finally:
                resumed.stop()


if __name__ == '__main__':
    unittest.main()
