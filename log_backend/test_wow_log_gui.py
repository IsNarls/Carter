import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__:
    from .encounter_parser import EncounterRef
    from .wow_log_gui import (fight_module_catalog, filter_checked_report_paths,
                              group_cross_report_encounters, guild_data_directory,
                              newest_report_keys,
                              prepare_fight_module, report_modified_text, report_path_key,
                              read_console_update, redact_console_text, saved_guild_ids,
                              import_update_is_active,
                              parse_warcraft_import_progress, warcraft_import_progress_lines,
                              selectable_report_paths,
                              validate_learner_selection)
else:
    from encounter_parser import EncounterRef
    from wow_log_gui import (fight_module_catalog, filter_checked_report_paths,
                             group_cross_report_encounters, guild_data_directory,
                             newest_report_keys,
                             prepare_fight_module, report_modified_text, report_path_key,
                             read_console_update, redact_console_text, saved_guild_ids,
                             import_update_is_active,
                             parse_warcraft_import_progress, warcraft_import_progress_lines,
                             selectable_report_paths,
                             validate_learner_selection)


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
