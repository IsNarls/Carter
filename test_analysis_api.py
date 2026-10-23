import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from urllib.error import HTTPError
from urllib.request import urlopen

from analysis_api import create_server
from analysis_service import AnalysisService, ServiceError
from death_answer import build_death_answer
from encounter_parser import analyze_encounter, scan_encounters
from warcraft_logs_importer import metadata_path
from test_timer_export import sample_draft


def fixture_report():
    report = {
        'code': 'ApiTest01', 'title': 'API fixture', 'startTime': 0, 'endTime': 6000,
        'fights': [{'id': 11, 'encounterID': 99, 'name': 'Test Boss', 'startTime': 1000,
                    'endTime': 6000, 'kill': False, 'difficulty': 16, 'size': 2}],
        'masterData': {'actors': [
            {'id': 1, 'name': 'Alex', 'type': 'Player', 'subType': 'Mage'},
            {'id': 3, 'name': 'Alexandra', 'type': 'Player', 'subType': 'Priest'},
            {'id': 2, 'name': 'Test Boss', 'type': 'NPC', 'subType': 'Boss'},
        ], 'abilities': [
            {'gameID': 100, 'name': 'Heavy Slam'}, {'gameID': 101, 'name': 'Tiny Burn'},
            {'gameID': 102, 'name': 'Long Cast'}, {'gameID': 103, 'name': 'Heal'},
        ]}}
    events = [
        {'timestamp': 1500, 'type': 'damage', 'sourceID': 1, 'targetID': 2,
         'abilityGameID': 100, 'amount': 100, 'targetResources': {
             'hitPoints': 900, 'maxHitPoints': 1000, 'x': 0, 'y': 0, 'mapID': 7}},
        {'timestamp': 3000, 'type': 'begincast', 'sourceID': 2, 'targetID': 1,
         'abilityGameID': 102, 'resourceActor': 1, 'x': 10, 'y': 0, 'mapID': 7},
        {'timestamp': 3300, 'type': 'damage', 'sourceID': 2, 'targetID': 1,
         'abilityGameID': 100, 'amount': 90, 'targetResources': {
             'hitPoints': 10, 'maxHitPoints': 100, 'x': 1, 'y': 0, 'mapID': 7}},
        {'timestamp': 3400, 'type': 'heal', 'sourceID': 3, 'targetID': 1,
         'abilityGameID': 103, 'amount': 20, 'absorbedHealing': 5,
         'sourceResources': {'x': 3, 'y': 0, 'mapID': 7}},
        {'timestamp': 3900, 'type': 'damage', 'sourceID': 2, 'targetID': 1,
         'abilityGameID': 101, 'amount': 12, 'overkill': 2,
         'targetResources': {'hitPoints': 0, 'maxHitPoints': 100, 'x': 2, 'y': 0, 'mapID': 7}},
        {'timestamp': 4000, 'type': 'death', 'targetID': 1, 'killerID': 2,
         'killingAbilityGameID': 101},
        # This must never become the death position.
        {'timestamp': 4100, 'type': 'damage', 'sourceID': 2, 'targetID': 1,
         'abilityGameID': 101, 'amount': 1, 'targetResources': {
             'hitPoints': 0, 'maxHitPoints': 100, 'x': 99, 'y': 99, 'mapID': 7}},
        {'timestamp': 4500, 'type': 'cast', 'sourceID': 2, 'targetID': 1,
         'abilityGameID': 102},
    ]
    return report, events


class AnalysisApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.root = Path(self.temp.name)
        report, events = fixture_report()
        self.cache = self.root / 'warcraft-logs-ApiTest01.wcl.json'
        self.cache.write_text(json.dumps({'format': 'wow-log-tools-warcraft-logs-v1',
                                          'report': report, 'eventsByFight': {'11': events}}),
                              encoding='utf-8')
        metadata_path(self.cache).write_text(json.dumps({
            'format': 'wow-log-tools-metadata-v1', 'report': {
                key: report[key] for key in ('code', 'title', 'startTime', 'endTime')},
            'fights': report['fights']}), encoding='utf-8')
        analyze_encounter(self.cache, scan_encounters(self.cache)[0])
        self.service = AnalysisService(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_queries_death_context_and_limits(self):
        self.assertEqual(self.service.get_encounter('ApiTest01', 11)['name'], 'Test Boss')
        summary = self.service.get_fight_summary('ApiTest01', 11)
        self.assertEqual(summary['deaths'], 1)
        self.assertEqual(self.service.get_damage_ranking('ApiTest01', 11, 999)['limit'], 50)
        self.assertEqual(self.service.find_players('ApiTest01', 11, 'alex')['players'][0]['name'],
                         'Alex')
        with self.assertRaises(ServiceError) as raised:
            self.service.get_player_performance('ApiTest01', 11, player_name='ale')
        self.assertEqual(raised.exception.code, 'AMBIGUOUS_PLAYER')
        deaths = self.service.list_deaths('ApiTest01', 11)['deaths']
        self.assertNotIn('damage_before_death', deaths[0])
        detail = self.service.get_death_analysis('ApiTest01', 11, deaths[0]['death_id'],
                                                 seconds_before=100)
        self.assertEqual(detail['seconds_before'], 30.0)
        self.assertEqual(detail['killing_blow']['provenance'], 'reported')
        self.assertEqual(detail['primary_cause']['ability'], 'Heavy Slam')
        self.assertEqual(detail['position']['x'], 2)
        self.assertEqual(detail['position']['confidence'], 'high')
        self.assertEqual(detail['spatial_context']['players_within_5_yards'], 1)
        self.assertTrue(detail['active_mechanics'])
        self.assertEqual(detail['damage_before_death'][-1]['health_before'], 10)
        timeline = self.service._timeline('ApiTest01', 11)
        self.assertEqual(timeline.rows[2].boss_positions['WCL-2'].x, 10)
        replay = self.service.get_death_replay('ApiTest01', 11, deaths[0]['death_id'],
                                               100, 100, 1)
        self.assertLessEqual(replay['end'] - replay['start'], 30)
        self.assertEqual(replay['resolution_ms'], 100)

    def test_npc_instances_remain_distinct(self):
        report, _events = fixture_report()
        report['fights'][0]['id'] = 12
        path = self.root / 'instances.wcl.json'
        events = [
            {'timestamp': 1500, 'type': 'damage', 'sourceID': 1, 'targetID': 2,
             'targetInstance': instance, 'amount': 10,
             'targetResources': {'hitPoints': 90, 'maxHitPoints': 100}}
            for instance in (1, 2)
        ]
        path.write_text(json.dumps({'format': 'wow-log-tools-warcraft-logs-v1',
                                    'report': report, 'eventsByFight': {'12': events}}),
                        encoding='utf-8')
        timeline = analyze_encounter(path, scan_encounters(path)[0])
        self.assertEqual({boss.guid for boss in timeline.bosses}, {'WCL-2:1', 'WCL-2:2'})

    def test_death_answer_bundles_evidence_and_image(self):
        death_id = self.service.list_deaths('ApiTest01', 11)['deaths'][0]['death_id']
        payload = self.service.get_death_answer('ApiTest01', 11, death_id)
        self.assertIn('Heavy Slam', payload['answer']['text'])
        self.assertIn('Tiny Burn', payload['answer']['text'])
        self.assertEqual(payload['analysis']['position']['x'], 2)
        self.assertEqual(payload['image']['status'], 'available')
        svg = base64.b64decode(payload['image']['data']).decode()
        ElementTree.fromstring(svg)
        self.assertIn('Alex', svg)
        self.assertEqual(payload['replay']['end'], payload['analysis']['time_seconds'])
        for samples in payload['replay']['actors'].values():
            self.assertTrue(all(p['t'] <= payload['analysis']['time_seconds'] for p in samples))
        self.assertEqual(payload['image']['map_id'], 7)
        server = create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (f'http://127.0.0.1:{server.server_port}/api/v1/reports/'
                   f'ApiTest01/fights/11/deaths/{death_id}/answer?seconds_before=100')
            with urlopen(url) as response:
                result = json.load(response)
            self.assertEqual(result['analysis']['seconds_before'], 30)
            self.assertEqual(result['image']['mime_type'], 'image/svg+xml')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_map_handles_missing_stale_other_floor_and_untrusted_names(self):
        death_id = self.service.list_deaths('ApiTest01', 11)['deaths'][0]['death_id']
        payload = self.service.get_death_answer('ApiTest01', 11, death_id)
        analysis, replay = payload['analysis'], payload['replay']
        replay['actors'] = {
            'other-floor': [{'t': 3, 'x': 10, 'y': 10, 'map_id': 8, 'sample_age_seconds': 0}],
            'stale': [{'t': 0, 'x': 10, 'y': 10, 'map_id': 7, 'sample_age_seconds': 4}],
            'future': [{'t': 4, 'x': 99, 'y': 99, 'map_id': 7, 'sample_age_seconds': 0}]}
        analysis['player']['name'] = '<script>unsafe</script>'
        result = build_death_answer(analysis, replay, {})
        svg = base64.b64decode(result['image']['data']).decode()
        root = ElementTree.fromstring(svg)
        self.assertEqual(len(root.findall('.//{http://www.w3.org/2000/svg}circle')), 0)
        self.assertNotIn('<script>', svg)
        analysis['position'] = None
        result = build_death_answer(analysis, replay, {})
        self.assertEqual(result['image']['status'], 'unavailable')
        self.assertIn('Tiny Burn', result['answer']['text'])

    def test_live_answer_uses_one_snapshot(self):
        timeline = self.service._timeline('ApiTest01', 11)
        death_id = timeline.deaths[0].death_id
        with patch.object(self.service, '_live_snapshot', return_value=(timeline, {})) as snapshot:
            with patch.object(self.service, '_live_base', return_value={'schema_version': 1}):
                result = self.service.get_live_death_answer(death_id)
        snapshot.assert_called_once()
        self.assertEqual(result['death_id'], death_id)
        self.assertEqual(result['image']['status'], 'available')

    def test_restricted_storage_and_structured_errors(self):
        with self.assertRaises(ServiceError) as raised:
            self.service.get_report('../ApiTest01')
        self.assertEqual(raised.exception.code, 'INVALID_REPORT_ID')
        with self.assertRaises(ServiceError) as raised:
            self.service.get_report('Missing01')
        self.assertEqual(raised.exception.response()['error']['code'], 'REPORT_NOT_FOUND')

    def test_http_success_and_not_found(self):
        server = create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/v1'
        try:
            with urlopen(base + '/reports/ApiTest01/fights/11/summary') as response:
                payload = json.load(response)
            self.assertEqual(payload['fight_id'], 11)
            with self.assertRaises(HTTPError) as raised:
                urlopen(base + '/missing')
            self.assertEqual(raised.exception.code, 404)
            self.assertEqual(json.load(raised.exception)['error']['code'], 'ENDPOINT_NOT_FOUND')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_timer_export_endpoint(self):
        drafts = self.root / 'mechanic-drafts'
        drafts.mkdir()
        (drafts / 'sample-draft.json').write_text(
            json.dumps(sample_draft()), encoding='utf-8')
        server = create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (f'http://127.0.0.1:{server.server_port}/api/v1/'
                   'mechanic-drafts/sample-draft/timer-export')
            with urlopen(url) as response:
                payload = json.load(response)
            self.assertEqual(payload['format'], 'wow-fight-timer/v1')
            self.assertEqual(payload['triggers']['start']['emit']['type'], 'FIGHT_ENGAGED')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
