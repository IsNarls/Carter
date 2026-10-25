import json
import gzip
import io
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

if __package__:
    from .encounter_parser import analyze_encounter, scan_encounters
    from .warcraft_logs_importer import (WarcraftLogsClient, download_report_batch,
                                         extract_report_code, load_cache, load_credentials,
                                         analysis_directory, prepare_report_batch,
                                         prune_report_caches, metadata_path,
                                         download_checkpoint_path,
                                         WarcraftLogsRateLimitError)
else:
    from encounter_parser import analyze_encounter, scan_encounters
    from warcraft_logs_importer import (WarcraftLogsClient, download_report_batch,
                                        extract_report_code, load_cache, load_credentials,
                                        analysis_directory, prepare_report_batch,
                                        prune_report_caches, metadata_path,
                                        download_checkpoint_path,
                                        WarcraftLogsRateLimitError)


def sample_report():
    return {
        'code': 'AbCd1234', 'title': 'Test Report', 'startTime': 0, 'endTime': 5000,
        'fights': [{
            'id': 7, 'encounterID': 99, 'name': 'API Boss', 'startTime': 1000,
            'endTime': 5000, 'kill': True, 'difficulty': 16, 'size': 2,
        }],
        'masterData': {
            'actors': [
                {'id': 1, 'name': 'Hero', 'type': 'Player', 'subType': 'Mage',
                 'petOwner': None, 'gameID': 0},
                {'id': 2, 'name': 'API Boss', 'type': 'NPC', 'subType': 'Boss',
                 'petOwner': None, 'gameID': 123},
            ],
            'abilities': [{'gameID': 456, 'name': 'Big Blast', 'type': 4}],
        },
    }


EVENTS = [
    {'timestamp': 1200, 'type': 'begincast', 'sourceID': 2, 'targetID': 1,
     'abilityGameID': 456, 'x': 5, 'y': 6, 'mapID': 10},
    {'timestamp': 1700, 'type': 'cast', 'sourceID': 2, 'targetID': 1,
     'abilityGameID': 456},
    {'timestamp': 2000, 'type': 'damage', 'sourceID': 1, 'targetID': 2,
     'abilityGameID': 123, 'amount': 100, 'overkill': 0,
     'targetResources': {'hitPoints': 900, 'maxHitPoints': 1000,
                         'x': 11, 'y': 22, 'mapID': 10}},
    {'timestamp': 2500, 'type': 'damage', 'sourceID': 2, 'targetID': 1,
     'abilityGameID': 456, 'amount': 25,
     'targetResources': {'hitPoints': 475, 'maxHitPoints': 500,
                         'x': 33, 'y': 44, 'mapID': 10}},
    {'timestamp': 3000, 'type': 'heal', 'sourceID': 1, 'targetID': 1,
     'abilityGameID': 789, 'amount': 40, 'absorbedHealing': 10},
]


class FakeClient(WarcraftLogsClient):
    def authenticate(self):
        self.access_token = 'test'

    def graphql(self, query, variables):
        if 'query Report' in query:
            return {'reportData': {'report': sample_report()}}
        if variables['start'] == 1000:
            return {'reportData': {'report': {'events': {
                'data': EVENTS[:2], 'nextPageTimestamp': 1701,
            }}}}
        return {'reportData': {'report': {'events': {
            'data': EVENTS[2:], 'nextPageTimestamp': None,
        }}}}


class WarcraftLogsImporterTests(unittest.TestCase):
    def test_load_credentials_from_env_file(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            env_file = Path(folder) / '.env'
            env_file.write_text(
                'WARCRAFT_LOGS_CLIENT_ID=sample-id\n'
                'WARCRAFT_LOGS_CLIENT_SECRET="sample-secret"\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_credentials(env_file), ('sample-id', 'sample-secret'))

    def test_default_credentials_use_carter_prefixed_values_from_root_env(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            env_file = Path(folder) / '.env'
            env_file.write_text(
                'CARTER_WARCRAFT_LOGS_CLIENT_ID=root-id\n'
                'CARTER_WARCRAFT_LOGS_CLIENT_SECRET="root-secret"\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
                    load_credentials.__globals__, {'PROJECT_ENV_FILE': env_file}):
                self.assertEqual(load_credentials(), ('root-id', 'root-secret'))

    def test_process_credentials_override_root_env_without_logging_values(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            env_file = Path(folder) / '.env'
            env_file.write_text(
                'CARTER_WARCRAFT_LOGS_CLIENT_ID=file-id\n'
                'CARTER_WARCRAFT_LOGS_CLIENT_SECRET=file-secret\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {
                    'WARCRAFT_LOGS_CLIENT_ID': 'process-id',
                    'WARCRAFT_LOGS_CLIENT_SECRET': 'process-secret',
            }, clear=True):
                self.assertEqual(
                    load_credentials(env_file), ('process-id', 'process-secret'))

    def test_extract_report_code(self):
        self.assertEqual(extract_report_code(
            'https://www.warcraftlogs.com/reports/AbCd1234#fight=7'), 'AbCd1234')
        self.assertEqual(extract_report_code('AbCd1234'), 'AbCd1234')
        with self.assertRaises(ValueError):
            extract_report_code('not a report')

    def test_http_429_is_a_safe_rate_limit_signal(self):
        error = urllib.error.HTTPError(
            'https://example.invalid', 429, 'private upstream message',
            {'Retry-After': '17'}, io.BytesIO(b'{"private":"body"}'),
        )
        request = urllib.request.Request('https://example.invalid')
        with mock.patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(WarcraftLogsRateLimitError) as raised:
                WarcraftLogsClient('id', 'secret')._open_json(request)
        self.assertEqual(raised.exception.retry_after_seconds, 17)
        self.assertNotIn('private', str(raised.exception))

    def test_http_json_requests_and_decodes_gzip(self):
        payload = gzip.compress(json.dumps({'ready': True}).encode('utf-8'))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Encoding': 'gzip'}
        response.read.return_value = payload
        request = urllib.request.Request('https://example.invalid')
        with mock.patch('urllib.request.urlopen', return_value=response):
            self.assertEqual(
                WarcraftLogsClient('id', 'secret')._open_json(request),
                {'ready': True},
            )

    def test_prepare_report_batch_limits_and_rejects_duplicates(self):
        prepared = prepare_report_batch([
            'https://www.warcraftlogs.com/reports/Report001', '', 'Report002'])
        self.assertEqual([code for _value, code in prepared], ['Report001', 'Report002'])
        with self.assertRaisesRegex(ValueError, 'already in this batch'):
            prepare_report_batch(['Report001', 'https://www.warcraftlogs.com/reports/Report001'])
        with self.assertRaisesRegex(ValueError, 'at most 5'):
            prepare_report_batch([f'Report{i:03d}' for i in range(6)])

    def test_batch_downloads_sequentially_and_continues_after_failure(self):
        calls = []

        class BatchClient:
            def download_report(self, report_value, destination, progress):
                code = extract_report_code(report_value)
                calls.append(code)
                progress('starting')
                if code == 'Fail0001':
                    raise RuntimeError('private report')
                Path(destination).write_text(code, encoding='utf-8')
                return destination

        with tempfile.TemporaryDirectory(dir='.') as folder:
            messages = []
            completed, failures = download_report_batch(
                BatchClient(), ['Report001', 'Fail0001', 'Report002'], Path(folder),
                messages.append)
            self.assertEqual(calls, ['Report001', 'Fail0001', 'Report002'])
            self.assertEqual([path.name for path in completed], [
                'warcraft-logs-Report001.wcl.json', 'warcraft-logs-Report002.wcl.json'])
            self.assertEqual(failures, [{'report_code': 'Fail0001',
                                         'error': 'private report'}])
            self.assertTrue(any('Report 3/3' in message for message in messages))

    def test_batch_reuses_valid_cache_and_stops_after_rate_limit(self):
        calls = []

        class BatchClient:
            def download_report(self, report_value, destination, progress):
                calls.append(extract_report_code(report_value))
                raise WarcraftLogsRateLimitError(20)

        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            cached = root / 'warcraft-logs-Cached001.wcl.json'
            _report = sample_report()
            cached.write_text(json.dumps({
                'format': 'wow-log-tools-warcraft-logs-v1',
                'report': _report,
                'eventsByFight': {'7': EVENTS},
            }), encoding='utf-8')
            completed, failures = download_report_batch(
                BatchClient(), ['Cached001', 'Limited01', 'NeverRun2'], root,
            )
            self.assertEqual(completed, [cached])
            self.assertEqual(calls, ['Limited01'])
            self.assertEqual(failures[0]['report_code'], 'Limited01')
            self.assertTrue(failures[0]['retryable'])

    def test_download_paginates_and_writes_cache(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            destination = Path(folder) / 'report.wcl.json'
            messages = []
            FakeClient('id', 'secret').download_report('AbCd1234', destination, messages.append)
            cache = load_cache(destination)
            self.assertEqual(len(cache['eventsByFight']['7']), len(EVENTS))
            self.assertTrue(metadata_path(destination).is_file())
            self.assertTrue(any('Imported 5 events' in message for message in messages))

    def test_download_debug_prints_page_cursor_and_counts_to_stderr(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            destination = Path(folder) / 'report.wcl.json'
            output = io.StringIO()
            progress = []
            with mock.patch.dict(os.environ, {'WARCRAFT_LOGS_DOWNLOAD_DEBUG': '1'}):
                with redirect_stderr(output):
                    FakeClient('id', 'secret').download_report(
                        'AbCd1234', destination, progress.append)

            messages = output.getvalue()
            self.assertIn('page=1 start=1000 end=5000 events_before=0 request_started', messages)
            self.assertIn('returned_events=2 collected_events=2 next_start=1701', messages)
            self.assertIn('page=2 start=1701 end=5000 events_before=2 request_started', messages)
            self.assertIn('returned_events=3 collected_events=5 next_start=None', messages)
            self.assertNotIn('secret', messages)
            self.assertTrue(any(
                '[debug] report=AbCd1234 fight=7 pull=1/1 page=1' in item
                for item in progress))

    def test_rate_limited_download_resumes_from_atomic_page_checkpoint(self):
        class LimitedClient(FakeClient):
            def graphql(self, query, variables):
                if 'query Report' in query:
                    return super().graphql(query, variables)
                if variables['start'] == 1000:
                    return {'reportData': {'report': {'events': {
                        'data': EVENTS[:2], 'nextPageTimestamp': 1701,
                    }}}}
                raise WarcraftLogsRateLimitError(30)

        class ResumeClient(FakeClient):
            starts = []

            def graphql(self, query, variables):
                if 'query Report' in query:
                    raise AssertionError('resume should use checkpoint metadata')
                self.starts.append(variables['start'])
                return {'reportData': {'report': {'events': {
                    'data': EVENTS[2:], 'nextPageTimestamp': None,
                }}}}

        with tempfile.TemporaryDirectory(dir='.') as folder:
            destination = Path(folder) / 'report.wcl.json'
            destination.write_text('previous complete cache remains intact', encoding='utf-8')
            previous = destination.read_bytes()
            with self.assertRaises(WarcraftLogsRateLimitError):
                LimitedClient('id', 'secret').download_report('AbCd1234', destination)
            checkpoint = download_checkpoint_path(destination)
            self.assertTrue(checkpoint.is_file())
            self.assertEqual(destination.read_bytes(), previous)
            if os.name == 'posix':
                self.assertEqual(checkpoint.stat().st_mode & 0o077, 0)
            saved = json.loads(checkpoint.read_text(encoding='utf-8'))
            self.assertEqual(saved['current']['next_start'], 1701.0)
            self.assertEqual(len(saved['current']['events']), 2)

            resumed = ResumeClient('id', 'secret')
            resumed.download_report('AbCd1234', destination)

            self.assertEqual(resumed.starts, [1701.0])
            self.assertEqual(len(load_cache(destination)['eventsByFight']['7']), len(EVENTS))
            self.assertFalse(checkpoint.exists())

    def test_imported_report_uses_existing_analysis_model(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            destination = Path(folder) / 'report.wcl.json'
            destination.write_text(json.dumps({
                'format': 'wow-log-tools-warcraft-logs-v1',
                'report': sample_report(),
                'eventsByFight': {'7': EVENTS},
            }), encoding='utf-8')
            encounters = scan_encounters(destination)
            self.assertEqual(len(encounters), 1)
            timeline = analyze_encounter(destination, encounters[0])
            self.assertTrue(timeline.encounter.success)
            self.assertEqual(timeline.bosses[0].name, 'API Boss')
            self.assertEqual(timeline.bosses[0].damage, 100)
            self.assertEqual(timeline.rows[1].health, 900)
            self.assertEqual(timeline.players[0].name, 'Hero')
            self.assertEqual(timeline.players[0].class_name, 'Mage')
            self.assertEqual(timeline.players[0].damage, 100)
            self.assertEqual(timeline.players[0].dps, 25)
            self.assertEqual(timeline.players[0].healing, 50)
            self.assertEqual(timeline.players[0].hps, 12.5)
            self.assertEqual(timeline.rows[1].player_positions['WCL-1'].x, 33)
            self.assertEqual(timeline.rows[1].boss_positions['WCL-2'].x, 11)
            self.assertEqual(timeline.mechanics[0].ability, 'Big Blast')
            self.assertAlmostEqual(timeline.mechanics[0].end - timeline.mechanics[0].start, 0.5)
            saved = analysis_directory(destination) / 'fight-7.json.gz'
            self.assertTrue(saved.is_file())
            module = f'{__package__}.encounter_parser' if __package__ else 'encounter_parser'
            with mock.patch(f'{module}.analyze_warcraft_logs_encounter',
                            side_effect=AssertionError('analysis should load from disk')):
                restored = analyze_encounter(destination, encounters[0])
            self.assertEqual(restored.players[0].damage, 100)

    def test_report_retention_uses_raid_time_and_removes_analysis(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            caches = []
            for index in range(6):
                cache = root / f'warcraft-logs-Report{index:02d}.wcl.json'
                cache.write_text('{}', encoding='utf-8')
                # File download order deliberately opposes report chronology.
                timestamp = 1_700_000_000 + (5 - index)
                os.utime(cache, (timestamp, timestamp))
                sidecar = analysis_directory(cache)
                sidecar.mkdir()
                (sidecar / 'fight-1.json.gz').write_text('saved', encoding='utf-8')
                metadata_path(cache).write_text(json.dumps({
                    'report': {'startTime': index * 1000, 'endTime': index * 1000 + 500},
                }), encoding='utf-8')
                caches.append(cache)
            removed = prune_report_caches(root)
            self.assertEqual(removed, [caches[0].resolve()])
            self.assertFalse(caches[0].exists())
            self.assertFalse(analysis_directory(caches[0]).exists())
            self.assertFalse(metadata_path(caches[0]).exists())
            self.assertTrue(all(path.exists() for path in caches[1:]))


if __name__ == '__main__':
    unittest.main()
