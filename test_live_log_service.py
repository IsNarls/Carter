import csv
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from analysis_api import create_server
from analysis_service import AnalysisService, ServiceError
from live_log_service import LiveLogService
from mechanic_timeline import MechanicScriptRegistry


def line(stamp, fields):
    output = io.StringIO()
    csv.writer(output, lineterminator='').writerow(fields)
    return f'4/8/2026 20:00:{stamp}-7  {output.getvalue()}\n'


START = line('00.000', ['ENCOUNTER_START', 1, 'Test Boss', 15, 2, 999])
END = line('06.000', ['ENCOUNTER_END', 1, 'Test Boss', 15, 2, 0, 6000])


def spell(stamp, source_guid, source_name, target_guid, target_name, spell_id,
          ability, health, maximum, amount, x, y, map_id, kind='SPELL_DAMAGE'):
    snapshot = [target_guid, 0, health, maximum, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                x, y, map_id, 0, 0, amount, amount, 0]
    return line(stamp, [kind, source_guid, source_name, 0, 0, target_guid, target_name,
                        0, 0, spell_id, ability, 1, *snapshot])


BOSS_HIT = spell('01.000', 'Player-1', 'Hero', 'Creature-1', 'Test Boss',
                 10, 'Bolt', 900, 1000, 100, 10, 10, 7)
BIG_HIT = spell('03.000', 'Creature-1', 'Test Boss', 'Player-1', 'Hero',
                20, 'Slam', 10, 500, 490, 2, 2, 7)
TINY_HIT = spell('04.000', 'Creature-1', 'Test Boss', 'Player-1', 'Hero',
                 21, 'Burn', 0, 500, 10, 3, 2, 7, 'SPELL_PERIODIC_DAMAGE')
FUTURE_HIT = spell('04.200', 'Creature-1', 'Test Boss', 'Player-1', 'Hero',
                   21, 'Burn', 0, 500, 1, 99, 99, 7, 'SPELL_PERIODIC_DAMAGE')
DEATH = line('04.100', ['UNIT_DIED', 'nil', 'nil', 0, 0, 'Player-1', 'Hero', 0, 0])
CAST_START = line('02.000', ['SPELL_CAST_START', 'Creature-1', 'Test Boss', 0, 0,
                            'Player-1', 'Hero', 0, 0, 30, 'Long Cast', 1])
CAST_END = line('05.000', ['SPELL_CAST_SUCCESS', 'Creature-1', 'Test Boss', 0, 0,
                          'Player-1', 'Hero', 0, 0, 30, 'Long Cast', 1])
HEAL = spell('03.500', 'Player-1', 'Hero', 'Player-1', 'Hero', 40, 'Heal',
             50, 500, 25, 2, 2, 7, 'SPELL_HEAL')
DEBUFF = line('03.750', ['SPELL_AURA_APPLIED', 'Creature-1', 'Test Boss', 0, 0,
                        'Player-1', 'Hero', 0, 0, 41, 'Doom', 1, 'DEBUFF'])


class LiveLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.root = Path(self.temp.name)
        self.log = self.root / 'WoWCombatLog.txt'
        self.log.write_bytes(b'')
        self.live = LiveLogService(self.log, self.root, from_end=False, rebuild_interval=.01)
        self.service = AnalysisService(self.root, live=self.live)

    def tearDown(self):
        self.live.stop()
        self.temp.cleanup()

    def append(self, text):
        with self.log.open('a', encoding='utf-8', newline='') as output:
            output.write(text)
        self.live.scan_once()

    def test_timer_module_can_be_prearmed_before_live_encounter(self):
        registry = MechanicScriptRegistry([{
            'script_id': 'heroic-boss', 'encounter_id': 1,
            'encounter_name': 'Test Boss', 'difficulty': 15,
            'placeholder': False,
            'mechanics': [{'id': 'warning', 'time': 5, 'message': 'Prepare.'}],
        }])
        live = LiveLogService(self.log, self.root, from_end=False,
                              mechanic_registry=registry)
        service = AnalysisService(self.root, live=live)
        server = create_server(self.root, port=0, live=live)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/v1/live/timer-module'
            request = Request(
                base + '/arm', data=json.dumps({'encounter_id': 1, 'difficulty': 15}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request) as response:
                armed = json.load(response)
            self.assertEqual(armed['armed_script_id'], 'heroic-boss')
            self.log.write_text(START, encoding='utf-8')
            live.scan_once()
            timeline = service.get_live_timeline()
            self.assertEqual(timeline['script_id'], 'heroic-boss')
            self.assertEqual(timeline['events'][0]['difficulty'], 15)
            with urlopen(Request(base + '/disarm', data=b'{}', method='POST')) as response:
                disarmed = json.load(response)
            self.assertIsNone(disarmed['armed_script_id'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            live.stop()

    def test_incremental_partial_death_completion_and_recovery(self):
        self.log.write_text(START.rstrip('\n'), encoding='utf-8')
        self.assertEqual(self.live.scan_once(), 0)
        self.append('\n' + BOSS_HIT + CAST_START + BIG_HIT + HEAL + DEBUFF + TINY_HIT +
                    DEATH)
        summary = self.service.get_live_fight_summary()
        self.assertEqual(summary['encounter_state'], 'active')
        self.assertEqual(summary['deaths'], 1)
        deaths = self.service.list_live_deaths()['deaths']
        detail = self.service.get_live_death_analysis(death_id=deaths[0]['death_id'])
        self.assertEqual(detail['killing_blow']['ability'], 'Burn')
        self.assertEqual(detail['killing_blow']['provenance'], 'inferred')
        self.assertEqual(detail['primary_cause']['ability'], 'Slam')
        self.assertEqual(detail['position']['x'], 3)
        self.assertEqual(detail['healing_before_death'][0]['amount'], 25)
        self.assertEqual(detail['debuffs'][0]['ability'], 'Doom')
        self.assertEqual(detail['active_mechanics'][0]['ability'], 'Long Cast')
        replay = self.service.get_live_death_replay(deaths[0]['death_id'])
        self.assertLessEqual(replay['end'] - replay['start'], 5)
        self.assertEqual(replay['actors']['Player-1'][-1]['x'], 3)
        updates = self.service.get_live_updates(0, 2)
        self.assertEqual(len(updates['updates']), 2)
        self.append(FUTURE_HIT + CAST_END + END)
        self.assertEqual(self.service.get_live_status()['encounter_state'], 'completed')
        completed_replay = self.service.get_live_death_replay(deaths[0]['death_id'])
        self.assertEqual(completed_replay['actors']['Player-1'][-1]['x'], 3)
        self.assertEqual(len(self.service.list_live_completed_encounters()['encounters']), 1)
        self.assertEqual(self.service.get_live_completed_summary(1)['deaths'], 1)
        self.assertTrue((self.root / 'live-analysis' /
                         f'{self.live.session_id}-latest.json.gz').is_file())
        recovered = LiveLogService(self.log, self.root)
        try:
            self.assertEqual(AnalysisService(self.root, recovered).get_live_fight_summary()[
                'deaths'], 1)
            self.assertEqual(len(AnalysisService(
                self.root, recovered).list_live_completed_encounters()['encounters']), 1)
        finally:
            recovered.stop()

    def test_truncation_no_duplicates_and_http(self):
        self.append(START + BOSS_HIT)
        cursor = self.live.status()['cursor']
        self.live.scan_once()
        self.assertEqual(self.live.status()['cursor'], cursor)
        self.log.write_text('', encoding='utf-8')
        self.live.scan_once()
        self.assertEqual(self.live.status()['encounter_state'], 'idle')
        server = create_server(self.root, port=0, live=self.live)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/v1/live/status') as response:
                payload = json.load(response)
            self.assertEqual(payload['source_name'], 'WoWCombatLog.txt')
            self.assertNotIn(str(self.root), json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_file_replacement_starts_new_attempt(self):
        self.append(START + BOSS_HIT)
        replacement = self.root / 'replacement.txt'
        replacement.write_text(START + BOSS_HIT, encoding='utf-8')
        os.replace(replacement, self.log)
        self.live.scan_once()
        self.assertEqual(self.live.status()['encounter_state'], 'active')
        self.assertEqual(self.service.get_live_encounter()['attempt'], 2)

    def test_unconfigured_and_expired_cursor_errors(self):
        with self.assertRaises(ServiceError) as raised:
            AnalysisService(self.root).get_live_updates()
        self.assertEqual(raised.exception.code, 'LIVE_SOURCE_NOT_CONFIGURED')
        for index in range(5002):
            self.live._append_update(f'event-{index}')
        with self.assertRaises(ServiceError) as raised:
            self.service.get_live_updates(0)
        self.assertEqual(raised.exception.code, 'CURSOR_EXPIRED')


if __name__ == '__main__':
    unittest.main()
