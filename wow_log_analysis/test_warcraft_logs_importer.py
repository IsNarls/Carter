import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from encounter_parser import analyze_encounter, scan_encounters
from warcraft_logs_importer import (WarcraftLogsClient, download_report_batch,
                                    extract_report_code, load_cache, load_credentials,
                                    analysis_directory, prepare_report_batch,
                                    prune_report_caches, metadata_path)


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

    def test_extract_report_code(self):
        self.assertEqual(extract_report_code(
            'https://www.warcraftlogs.com/reports/AbCd1234#fight=7'), 'AbCd1234')
        self.assertEqual(extract_report_code('AbCd1234'), 'AbCd1234')
        with self.assertRaises(ValueError):
            extract_report_code('not a report')

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

    def test_download_paginates_and_writes_cache(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            destination = Path(folder) / 'report.wcl.json'
            messages = []
            FakeClient('id', 'secret').download_report('AbCd1234', destination, messages.append)
            cache = load_cache(destination)
            self.assertEqual(len(cache['eventsByFight']['7']), len(EVENTS))
            self.assertTrue(metadata_path(destination).is_file())
            self.assertTrue(any('Imported 5 events' in message for message in messages))

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
            with mock.patch('encounter_parser.analyze_warcraft_logs_encounter',
                            side_effect=AssertionError('analysis should load from disk')):
                restored = analyze_encounter(destination, encounters[0])
            self.assertEqual(restored.players[0].damage, 100)

    def test_report_retention_keeps_newest_five_and_removes_analysis(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            caches = []
            for index in range(6):
                cache = root / f'warcraft-logs-Report{index:02d}.wcl.json'
                cache.write_text('{}', encoding='utf-8')
                timestamp = 1_700_000_000 + index
                os.utime(cache, (timestamp, timestamp))
                sidecar = analysis_directory(cache)
                sidecar.mkdir()
                (sidecar / 'fight-1.json.gz').write_text('saved', encoding='utf-8')
                metadata_path(cache).write_text('{}', encoding='utf-8')
                caches.append(cache)
            removed = prune_report_caches(root)
            self.assertEqual(removed, [caches[0].resolve()])
            self.assertFalse(caches[0].exists())
            self.assertFalse(analysis_directory(caches[0]).exists())
            self.assertFalse(metadata_path(caches[0]).exists())
            self.assertTrue(all(path.exists() for path in caches[1:]))


if __name__ == '__main__':
    unittest.main()
