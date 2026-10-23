import json
import tempfile
import unittest
from pathlib import Path

from encounter_parser import EncounterRef
from wow_log_gui import group_cross_report_encounters, validate_learner_selection


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


if __name__ == '__main__':
    unittest.main()
