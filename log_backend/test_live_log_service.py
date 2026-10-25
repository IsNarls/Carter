import csv
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if __package__:
    from .analysis_api import create_server
    from .analysis_service import AnalysisService, ServiceError
    from .live_log_service import (LIVE_ACCESS_TOKEN_FILENAME, LiveLogService,
                                   ensure_live_access_token)
    from .mechanic_timeline import MechanicScriptRegistry
else:
    from analysis_api import create_server
    from analysis_service import AnalysisService, ServiceError
    from live_log_service import (LIVE_ACCESS_TOKEN_FILENAME, LiveLogService,
                                  ensure_live_access_token)
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
GUILD_ID = '123456789'


def guild_request(url, *, data=None, method=None, headers=None):
    scoped = dict(headers or {})
    scoped['X-Carter-Guild-ID'] = GUILD_ID
    return Request(url, data=data, headers=scoped, method=method)


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

    def test_live_access_token_is_private_persistent_and_never_returned(self):
        token_path = self.root / 'live-analysis' / LIVE_ACCESS_TOKEN_FILENAME
        token_value = token_path.read_text(encoding='ascii').strip()
        status = self.live.status()
        recovered = LiveLogService(self.log, self.root)
        try:
            self.assertTrue(status['access_token_ready'])
            self.assertEqual(
                status['access_token_fingerprint'], recovered.access_token_fingerprint)
            self.assertNotIn('access_token', status)
            self.assertNotIn(token_value, json.dumps(status))
            self.assertGreaterEqual(len(token_value), 40)
            if os.name == 'posix':
                self.assertEqual(token_path.stat().st_mode & 0o077, 0)
        finally:
            recovered.stop()

    def test_invalid_existing_live_token_is_not_overwritten(self):
        storage = self.root / 'invalid-token-storage'
        token_path = storage / 'live-analysis' / LIVE_ACCESS_TOKEN_FILENAME
        token_path.parent.mkdir(parents=True)
        token_path.write_text('existing-invalid-value', encoding='ascii')

        with self.assertRaisesRegex(ValueError, 'invalid'):
            LiveLogService(self.log, storage)

        self.assertEqual(token_path.read_text(encoding='ascii'), 'existing-invalid-value')

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
        server = create_server(self.root, port=0, live=live, live_guild_id=GUILD_ID)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/v1/live/timer-module'
            request = guild_request(
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
            with urlopen(guild_request(base + '/disarm', data=b'{}', method='POST')) as response:
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
        server = create_server(
            self.root, port=0, live=self.live, live_guild_id=GUILD_ID,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/v1/live/rankings'
            with urlopen(guild_request(base + '/dps')) as response:
                self.assertEqual(json.load(response)['metric'], 'dps')
            with urlopen(guild_request(base + '/hps')) as response:
                self.assertEqual(json.load(response)['metric'], 'hps')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
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

    def test_archived_attempt_queries_keep_identity_after_new_pull_and_restart(self):
        self.append(START + BOSS_HIT + CAST_START + BIG_HIT + HEAL + DEBUFF +
                    TINY_HIT + DEATH + CAST_END + END)
        session_id = self.live.session_id

        summary = self.service.get_live_attempt_summary(1)
        self.assertEqual(summary['session_id'], session_id)
        self.assertEqual(summary['attempt'], 1)
        self.assertEqual(summary['encounter_state'], 'completed')
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['deaths'], 1)
        self.assertEqual(summary['players'], 1)

        players = self.service.find_live_attempt_players(1, 'hero')
        self.assertEqual(players['players'][0]['name'], 'Hero')
        player = self.service.get_live_attempt_player_performance(1, player_name='Hero')
        self.assertEqual(player['player']['guid'], 'Player-1')
        damage = self.service.get_live_attempt_damage_ranking(1)
        healing = self.service.get_live_attempt_healing_ranking(1)
        dps = self.service.get_live_attempt_dps_ranking(1)
        hps = self.service.get_live_attempt_hps_ranking(1)
        self.assertEqual((damage['metric'], damage['players'][0]['name']), ('damage', 'Hero'))
        self.assertEqual((healing['metric'], healing['players'][0]['name']),
                         ('healing', 'Hero'))
        self.assertEqual((dps['metric'], dps['players'][0]['name']), ('dps', 'Hero'))
        self.assertEqual((hps['metric'], hps['players'][0]['name']), ('hps', 'Hero'))

        deaths = self.service.list_live_attempt_deaths(1, player_name='Hero')
        death_id = deaths['deaths'][0]['death_id']
        detail = self.service.get_live_attempt_death_analysis(1, death_id=death_id)
        self.assertEqual(detail['primary_cause']['ability'], 'Slam')
        answer = self.service.get_live_attempt_death_answer(1, death_id)
        self.assertEqual(answer['death_id'], death_id)
        self.assertIn('answer', answer)
        self.assertIn('analysis', answer)
        self.assertIn('replay', answer)
        self.assertIn('image', answer)
        for payload in (answer, answer['analysis'], answer['replay']):
            self.assertEqual(payload['session_id'], session_id)
            self.assertEqual(payload['attempt'], 1)
            self.assertEqual(payload['encounter_state'], 'completed')

        second_start = line('10.000', ['ENCOUNTER_START', 1, 'Test Boss', 15, 2, 999])
        self.append(second_start)
        self.assertEqual(self.service.get_live_encounter()['attempt'], 2)
        self.assertEqual(self.service.list_live_deaths()['deaths'], [])
        self.assertEqual(self.service.list_live_attempt_deaths(1)['deaths'][0]['death_id'],
                         death_id)

        server = create_server(
            self.root, port=0, live=self.live, live_guild_id=GUILD_ID,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/v1/live/attempts/1'
        try:
            with urlopen(guild_request(base + '/summary')) as response:
                self.assertEqual(json.load(response)['attempt'], 1)
            with urlopen(guild_request(base + '/players?query=hero')) as response:
                self.assertEqual(json.load(response)['players'][0]['name'], 'Hero')
            with urlopen(guild_request(base + '/players/Player-1')) as response:
                self.assertEqual(json.load(response)['player']['name'], 'Hero')
            with urlopen(guild_request(base + '/rankings/dps')) as response:
                self.assertEqual(json.load(response)['metric'], 'dps')
            with urlopen(guild_request(base + '/rankings/hps')) as response:
                self.assertEqual(json.load(response)['metric'], 'hps')
            with urlopen(guild_request(base + '/deaths')) as response:
                self.assertEqual(json.load(response)['deaths'][0]['death_id'], death_id)
            death_path = base + '/deaths/' + quote(death_id, safe='')
            with urlopen(guild_request(death_path)) as response:
                self.assertEqual(json.load(response)['attempt'], 1)
            with urlopen(guild_request(death_path + '/answer')) as response:
                api_answer = json.load(response)
            self.assertEqual(api_answer['death_id'], death_id)
            self.assertEqual(api_answer['analysis']['attempt'], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        second_big_hit = spell('13.000', 'Creature-1', 'Test Boss', 'Player-1',
                               'Hero', 20, 'Slam', 10, 500, 490, 2, 2, 7)
        second_tiny_hit = spell('14.000', 'Creature-1', 'Test Boss', 'Player-1',
                                'Hero', 21, 'Burn', 0, 500, 10, 3, 2, 7,
                                'SPELL_PERIODIC_DAMAGE')
        second_death = line('14.100', ['UNIT_DIED', 'nil', 'nil', 0, 0,
                                      'Player-1', 'Hero', 0, 0])
        self.append(second_big_hit + second_tiny_hit + second_death)
        abandoned_death_id = self.service.list_live_deaths()['deaths'][0]['death_id']

        self.live.stop()
        recovered = LiveLogService(self.log, self.root)
        try:
            archived = AnalysisService(self.root, recovered)
            restored = archived.get_live_attempt_death_answer(1, death_id)
            self.assertEqual(restored['session_id'], session_id)
            self.assertEqual(restored['attempt'], 1)
            self.assertEqual(restored['encounter_state'], 'completed')
            self.assertEqual(restored['analysis']['death_id'], death_id)
            abandoned = archived.get_live_attempt_death_answer(2, abandoned_death_id)
            self.assertEqual(abandoned['session_id'], session_id)
            self.assertEqual(abandoned['attempt'], 2)
            self.assertEqual(abandoned['encounter_state'], 'abandoned')
            self.assertFalse(abandoned['complete'])
            self.assertEqual(abandoned['analysis']['encounter_state'], 'abandoned')
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
        server = create_server(
            self.root, port=0, live=self.live, live_guild_id=GUILD_ID,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/v1/live'
            with urlopen(guild_request(base + '/status')) as response:
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

    def test_remote_ingest_requires_guild_token_and_deduplicates_retry(self):
        server = create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/v1/live/ingest'
            headers = {
                'Content-Type': 'application/octet-stream',
                'X-Carter-Live-Stream-ID': 'test-stream-001',
                'X-Carter-Live-Offset': '0',
            }
            with self.assertRaises(HTTPError) as raised:
                urlopen(guild_request(url, data=START.encode(), method='POST', headers=headers))
            self.assertEqual(raised.exception.code, 401)

            guild_storage = self.root / 'guilds' / GUILD_ID
            self.assertFalse(guild_storage.exists())
            token_path = ensure_live_access_token(guild_storage)['path']
            token = token_path.read_text(encoding='ascii').strip()
            headers['X-Carter-Live-Token'] = token
            data = (START + BOSS_HIT + END).encode()
            with urlopen(guild_request(url, data=data, method='POST', headers=headers)) as response:
                accepted = json.load(response)
            self.assertEqual(accepted['accepted'], 3)
            self.assertFalse(accepted['duplicate'])
            with urlopen(guild_request(url, data=data, method='POST', headers=headers)) as response:
                duplicate = json.load(response)
            self.assertTrue(duplicate['duplicate'])
            self.assertEqual(duplicate['accepted'], 0)

            status_url = f'http://127.0.0.1:{server.server_port}/api/v1/live/status'
            with urlopen(guild_request(status_url)) as response:
                status = json.load(response)
            self.assertEqual(status['source_type'], 'remote_live_log')
            self.assertEqual(status['encounter_state'], 'completed')
            self.assertNotIn(token, json.dumps(status))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_remote_ingest_recovers_an_active_pull_after_backend_restart(self):
        storage = self.root / 'remote-restart'
        first = LiveLogService(None, storage, remote=True, rebuild_interval=.01)
        opening = (START + BOSS_HIT).encode()
        first.ingest_remote_batch('restart-stream-001', 0, opening)
        self.assertEqual(first.status()['encounter_state'], 'active')

        first.stop()
        resumed = LiveLogService(None, storage, remote=True, rebuild_interval=.01)
        try:
            self.assertEqual(resumed.status()['encounter_state'], 'active')
            result = resumed.ingest_remote_batch(
                'restart-stream-001', len(opening), (BIG_HIT + END).encode(),
            )
            self.assertEqual(result['accepted'], 2)
            self.assertEqual(resumed.status()['encounter_state'], 'completed')
            attempts = AnalysisService(storage, live=resumed).list_live_attempts()['attempts']
            self.assertEqual([item['attempt'] for item in attempts], [1])
        finally:
            resumed.stop()

        restored = LiveLogService(None, storage, remote=True)
        try:
            attempts = AnalysisService(storage, live=restored).list_live_attempts()['attempts']
            self.assertEqual([item['attempt'] for item in attempts], [1])
            self.assertEqual(restored.status()['encounter_state'], 'completed')
        finally:
            restored.stop()


if __name__ == '__main__':
    unittest.main()
