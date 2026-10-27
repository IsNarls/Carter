import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

if __package__:
    from .encounter_parser import EncounterRef
    from .wow_log_gui import (LivePipelineAPIError, fight_module_catalog,
                              filter_checked_report_paths,
                              fetch_live_pipeline_snapshot,
                              group_cross_report_encounters, guild_data_directory,
                              live_pipeline_completed_row, live_pipeline_timestamp,
                              live_pipeline_update_row, live_pipeline_sender_text,
                              newest_report_keys,
                              prepare_fight_module, report_modified_text, report_path_key,
                              read_console_update, redact_console_text, saved_guild_ids,
                              import_update_is_active,
                              parse_warcraft_import_progress, warcraft_import_progress_lines,
                              selectable_report_paths,
                              validate_learner_selection)
else:
    from encounter_parser import EncounterRef
    from wow_log_gui import (LivePipelineAPIError, fight_module_catalog,
                             filter_checked_report_paths,
                             fetch_live_pipeline_snapshot,
                             group_cross_report_encounters, guild_data_directory,
                             live_pipeline_completed_row, live_pipeline_timestamp,
                             live_pipeline_update_row, live_pipeline_sender_text,
                             newest_report_keys,
                             prepare_fight_module, report_modified_text, report_path_key,
                             read_console_update, redact_console_text, saved_guild_ids,
                             import_update_is_active,
                             parse_warcraft_import_progress, warcraft_import_progress_lines,
                             selectable_report_paths,
                             validate_learner_selection)


class FakeJSONResponse:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.content if limit < 0 else self.content[:limit]


class RecordingJSONOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeJSONResponse(response)


def json_http_error(url, status, payload):
    return HTTPError(
        url, status, 'test failure', {}, io.BytesIO(json.dumps(payload).encode('utf-8')))


def encounter(attempt, encounter_id=3429, difficulty=4, external_id=None):
    return EncounterRef(
        attempt=attempt,
        encounter_id=encounter_id,
        name='Test Boss',
        difficulty=difficulty,
        group_size=20,
        start=0,
        end=120,
        external_id=attempt if external_id is None else external_id,
    )


class LivePipelineGuiTests(unittest.TestCase):
    def test_backend_downtime_is_reported_as_a_safe_structured_error(self):
        opener = RecordingJSONOpener([URLError('private transport detail')])

        with self.assertRaises(LivePipelineAPIError) as raised:
            fetch_live_pipeline_snapshot('1562210615466917909', opener=opener)

        self.assertEqual(raised.exception.code, 'LIVE_BACKEND_UNAVAILABLE')
        self.assertNotIn('private transport detail', str(raised.exception))

    def test_snapshot_reads_only_loopback_get_endpoints_without_credentials(self):
        opener = RecordingJSONOpener([
            {'source_type': 'remote_live_log', 'configured': True, 'cursor': 2,
             'parse_errors': 0},
            {'updates': [{'cursor': 2, 'type': 'combat_event'}], 'truncated': False},
            json_http_error('http://127.0.0.1/live/encounter', 404, {
                'error': {'code': 'NO_ACTIVE_ENCOUNTER', 'message': 'No encounter.'},
            }),
            {'encounters': [{'attempt': 1, 'name': 'Test Boss', 'kill': False}]},
        ])

        snapshot = fetch_live_pipeline_snapshot('1562210615466917909', opener=opener)

        self.assertEqual(snapshot['status']['source_type'], 'remote_live_log')
        self.assertIsNone(snapshot['encounter'])
        self.assertEqual(snapshot['updates'][0]['cursor'], 2)
        self.assertEqual(snapshot['completed'][0]['name'], 'Test Boss')
        self.assertEqual(len(opener.requests), 4)
        for request, timeout in opener.requests:
            headers = {key.casefold(): value for key, value in request.header_items()}
            self.assertTrue(request.full_url.startswith('http://127.0.0.1:8765/api/v1/live/'))
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(headers['x-carter-guild-id'], '1562210615466917909')
            self.assertFalse(any('token' in key or 'authorization' in key for key in headers))
            self.assertEqual(timeout, 2.0)

    def test_snapshot_recovers_an_expired_update_cursor(self):
        opener = RecordingJSONOpener([
            {'source_type': 'remote_live_log', 'configured': True, 'cursor': 50},
            json_http_error('http://127.0.0.1/live/updates', 409, {
                'error': {'code': 'CURSOR_EXPIRED', 'message': 'Expired.',
                          'oldest_cursor': 41},
            }),
            {'updates': [{'cursor': 41, 'type': 'encounter_start'}], 'truncated': False},
            json_http_error('http://127.0.0.1/live/encounter', 404, {
                'error': {'code': 'NO_ACTIVE_ENCOUNTER', 'message': 'No encounter.'},
            }),
            {'encounters': []},
        ])

        snapshot = fetch_live_pipeline_snapshot(
            '1562210615466917909', since_cursor=12, opener=opener)

        update_urls = [request.full_url for request, _timeout in opener.requests
                       if '/live/updates?' in request.full_url]
        self.assertEqual(len(update_urls), 2)
        self.assertIn('since=12', update_urls[0])
        self.assertIn('since=40', update_urls[1])
        self.assertEqual(snapshot['updates'][0]['cursor'], 41)

    def test_live_rows_allowlist_structured_fields_and_hide_raw_values(self):
        update = live_pipeline_update_row({
            'timestamp': '2026-10-25T19:34:03+00:00',
            'type': 'combat_event', 'ability': 'Safe\nAbility', 'source': 'Player',
            'raw_line': 'private combat log line', 'access_token': 'secret-token',
        })
        completed = live_pipeline_completed_row({
            'attempt': 3, 'name': 'Test Boss', 'kill': True,
            'duration_seconds': 92.25, 'deaths': 1, 'private_path': '/private/path',
        })

        self.assertEqual(update[1], 'combat_event')
        self.assertIn('Safe Ability', update[2])
        self.assertNotIn('private combat log line', ' '.join(update))
        self.assertNotIn('secret-token', ' '.join(update))
        self.assertEqual(completed, ('3', 'Test Boss', 'Kill', '92.2s', '1'))
        self.assertNotIn('/private/path', ' '.join(completed))

        callout = live_pipeline_update_row({
            'timestamp': '2026-10-25T19:34:04+00:00',
            'type': 'MECHANIC_CALLOUT', 'encounter_name': 'Known Boss',
            'mechanic_id': 'group_soak', 'message': 'Group soak.',
            'evidence': 'private review notes', 'raw_line': 'private raw line',
        })
        self.assertEqual(callout[1], 'MECHANIC_CALLOUT')
        self.assertIn('Known Boss', callout[2])
        self.assertIn('Group soak.', callout[2])
        self.assertNotIn('private', ' '.join(callout))

    def test_live_timestamp_reports_recency(self):
        shown = live_pipeline_timestamp(
            '2026-10-25T19:34:03+00:00',
            datetime(2026, 10, 25, 19, 34, 33, tzinfo=timezone.utc),
        )
        self.assertIn('(30s ago)', shown)
        self.assertEqual(live_pipeline_timestamp(None), 'No events received')

    def test_sender_diagnostics_format_is_allowlisted(self):
        shown = live_pipeline_sender_text({
            'state': 'running', 'thread_alive': True, 'file_size': 1000,
            'sender_offset': 800, 'reader_offset': 950, 'pending_bytes': 200,
            'last_result': 'HTTP_200', 'heartbeat_age_seconds': 2.4,
            'private_path': 'C:/private/WoWCombatLog.txt',
            'access_token': 'do-not-show',
        })

        self.assertIn('Sender: running', shown)
        self.assertIn('pending: 200 B', shown)
        self.assertIn('heartbeat: 2s ago', shown)
        self.assertNotIn('private', shown)
        self.assertNotIn('do-not-show', shown)


class GuiLearnerSelectionTests(unittest.TestCase):
    def test_console_reader_tails_incrementally_and_handles_truncation(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            path = Path(folder) / 'carter.log'
            path.write_text('first line\n', encoding='utf-8')

            first, offset, reset = read_console_update(path)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('second line\n')
            second, offset, second_reset = read_console_update(path, offset)
            path.write_text('new\n', encoding='utf-8')
            third, _, third_reset = read_console_update(path, offset)

            self.assertEqual(first, 'first line\n')
            self.assertTrue(reset)
            self.assertEqual(second, 'second line\n')
            self.assertFalse(second_reset)
            self.assertEqual(third, 'new\n')
            self.assertTrue(third_reset)

    def test_console_reader_keeps_only_bounded_recent_complete_lines(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            path = Path(folder) / 'carter.log'
            path.write_text('old line\nmiddle line\nnewest line\n', encoding='utf-8')

            content, offset, reset = read_console_update(path, max_bytes=24)

            self.assertTrue(reset)
            self.assertEqual(offset, path.stat().st_size)
            self.assertNotIn('old line', content)
            self.assertIn('newest line', content)

    def test_console_redacts_common_secret_shapes(self):
        google_key = 'AIza' + ('A' * 35)
        discord_token = ('A' * 24) + '.' + ('B' * 6) + '.' + ('C' * 30)
        source = (f'CARTER_GEMINI_API_KEY={google_key}\n'
                  f'Authorization: Bearer bearer-secret\nDiscord token={discord_token}\n')

        result = redact_console_text(source)

        self.assertNotIn(google_key, result)
        self.assertNotIn('bearer-secret', result)
        self.assertNotIn(discord_token, result)
        self.assertGreaterEqual(result.count('[redacted'), 3)

    def test_warcraft_console_keeps_only_backend_import_progress(self):
        lines = (
            "backend startup complete\n"
            '2026-10-23T12:00:00+00:00 WCL_IMPORT_PROGRESS '
            '{"report_id":"AbCd1234","status":"running","stage":"download",'
            '"progress":{"percent":null,"completed_fights":0,"total_fights":null,'
            '"message":"Downloading pull 1/4"},"errors":[]}\n'
            "backend request completed\n"
        )

        filtered = warcraft_import_progress_lines(lines)
        updates = parse_warcraft_import_progress(filtered)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["report_id"], "AbCd1234")
        self.assertEqual(updates[0]["status"], "running")
        self.assertEqual(updates[0]["progress"]["percent"], None)
        self.assertEqual(updates[0]["_logged_at"], "2026-10-23T12:00:00+00:00")

    def test_old_unfinished_import_snapshot_is_not_active_after_restart(self):
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        update = {
            'status': 'running',
            '_logged_at': (now - timedelta(minutes=6)).isoformat(),
        }
        self.assertFalse(import_update_is_active(update, now))
        update['_logged_at'] = (now - timedelta(seconds=30)).isoformat()
        self.assertTrue(import_update_is_active(update, now))

    def test_live_token_storage_uses_valid_saved_guild_scope(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            settings = root / 'guild-settings.json'
            settings.write_text(json.dumps({
                'guilds': {'123456789': {'logs_channel_id': 10}, '../bad': {}},
            }), encoding='utf-8')
            self.assertEqual(saved_guild_ids(settings), ['123456789'])
            self.assertEqual(
                guild_data_directory(root / 'data', '123456789'),
                (root / 'data' / 'guilds' / '123456789').resolve(),
            )
            with self.assertRaises(ValueError):
                guild_data_directory(root / 'data', '../bad')

    def test_selectable_reports_only_returns_wcl_caches_newest_first(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            older = root / 'older.wcl.json'
            newer = root / 'newer.wcl.json'
            unsupported = root / 'combat-log.txt'
            for path in (older, newer, unsupported):
                path.write_text('{}', encoding='utf-8')
            os.utime(older, (1_700_000_000, 1_700_000_000))
            os.utime(newer, (1_700_000_100, 1_700_000_100))

            reports = selectable_report_paths(
                [older, unsupported, newer, older, root / 'missing.wcl.json'])
            checked = filter_checked_report_paths(
                reports, {report_path_key(older)})
            newest = newest_report_keys(reports, limit=1)

            self.assertEqual(reports, [newer, older])
            self.assertEqual(checked, [older])
            self.assertEqual(newest, {report_path_key(newer)})
            self.assertNotEqual(report_modified_text(newer), 'Unavailable')

    def test_returns_exact_selected_wcl_fight_ids(self):
        selected = [encounter(2, external_id=12), encounter(4, external_id=19),
                    encounter(7, external_id=31)]

        result = validate_learner_selection(Path('report.wcl.json'), selected)

        self.assertEqual(result, {
            'encounter_id': 3429,
            'difficulty': 4,
            'fight_ids': [12, 19, 31],
        })

    def test_rejects_incompatible_selection(self):
        with self.assertRaisesRegex(ValueError, 'at least three'):
            validate_learner_selection('report.wcl.json', [encounter(1), encounter(2)])
        with self.assertRaisesRegex(ValueError, 'same boss'):
            validate_learner_selection(
                'report.wcl.json', [encounter(1), encounter(2), encounter(3, encounter_id=99)])
        with self.assertRaisesRegex(ValueError, 'same difficulty'):
            validate_learner_selection(
                'report.wcl.json', [encounter(1), encounter(2), encounter(3, difficulty=5)])
        with self.assertRaisesRegex(ValueError, 'imported Warcraft Logs'):
            validate_learner_selection('local-log.txt', [encounter(1), encounter(2), encounter(3)])

    def test_rejects_missing_external_fight_id(self):
        selected = [encounter(1), encounter(2), encounter(3)]
        selected[-1].external_id = None

        with self.assertRaisesRegex(ValueError, 'no Warcraft Logs fight ID'):
            validate_learner_selection('report.wcl.json', selected)

    def test_groups_matching_encounters_across_report_caches(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            paths = []
            for report_index in range(2):
                path = Path(folder) / f'warcraft-logs-Report00{report_index}.wcl.json'
                fights = [{
                    'id': report_index + 1,
                    'encounterID': 3429,
                    'name': 'Shared Boss',
                    'difficulty': 4,
                    'size': 20,
                    'startTime': 0,
                    'endTime': 100000,
                    'kill': report_index == 1,
                }]
                if report_index == 0:
                    fights.append({**fights[0], 'id': 9, 'encounterID': 999,
                                   'name': 'Other Boss'})
                path.write_text(json.dumps({
                    'format': 'wow-log-tools-warcraft-logs-v1',
                    'report': {'code': f'Report00{report_index}', 'fights': fights},
                    'eventsByFight': {},
                }), encoding='utf-8')
                paths.append(path)

            groups = group_cross_report_encounters(paths)
            shared = next(group for group in groups if group['encounter_id'] == 3429)

            self.assertEqual(len(shared['pulls']), 2)
            self.assertEqual(len(shared['paths']), 2)
            self.assertEqual(sum(pull.success for pull in shared['pulls']), 1)


class FightModuleCatalogTests(unittest.TestCase):
    @staticmethod
    def draft():
        return {
            'schema_version': 1,
            'draft_id': 'test-boss-heroic-draft-v1',
            'status': 'DRAFT_REQUIRES_REVIEW',
            'pull_count': 4,
            'script': {
                'script_id': 'test boss:heroic',
                'encounter_id': 3429,
                'encounter_name': 'Test Boss',
                'difficulty': 4,
                'mechanics': [{
                    'id': 'raid-hit-warning',
                    'time': 12.5,
                    'kind': 'ABILITY_WARNING',
                    'message': 'Raid damage soon.',
                }],
            },
        }

    def test_prepares_draft_and_catalog_marks_it_ready(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            drafts = root / 'mechanic-drafts'
            modules = root / 'timer-exports'
            drafts.mkdir()
            draft_path = drafts / 'test-boss.json'
            draft_path.write_text(json.dumps(self.draft()), encoding='utf-8')

            before = fight_module_catalog(drafts, modules)
            destination, payload = prepare_fight_module(draft_path, modules)
            after = fight_module_catalog(drafts, modules)

            self.assertEqual(len(before), 1)
            self.assertFalse(before[0]['ready'])
            self.assertEqual(destination.name, 'test-boss-heroic.timer.json')
            self.assertTrue(payload['enabled'])
            self.assertTrue(destination.is_file())
            self.assertTrue(after[0]['ready'])
            self.assertEqual(after[0]['module_path'], destination)

    def test_invalid_draft_is_visible_but_cannot_be_ready(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            drafts = root / 'mechanic-drafts'
            drafts.mkdir()
            (drafts / 'broken.json').write_text('{broken', encoding='utf-8')

            items = fight_module_catalog(drafts, root / 'timer-exports')

            self.assertEqual(len(items), 1)
            self.assertFalse(items[0]['ready'])
            self.assertTrue(items[0]['error'])


if __name__ == '__main__':
    unittest.main()
