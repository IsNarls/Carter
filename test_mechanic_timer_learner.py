import json
import tempfile
import unittest
from pathlib import Path

from analysis_service import AnalysisService, ServiceError
from mechanic_timer_learner import learn_timer_script, save_draft
from mechanic_timeline import MechanicScriptRegistry
from timer_export import build_timer_export


def fixture_report(path):
    actors = [{'id': index, 'name': f'Player {index}', 'type': 'Player',
               'subType': 'Mage'} for index in range(1, 6)]
    actors.append({'id': 99, 'name': 'Timer Boss', 'type': 'NPC', 'subType': 'Boss'})
    fights = []
    events = {}
    high_times = [(20.0, 50.0), (20.4, 50.2), (19.7, 49.8), (20.2, 50.4)]
    unstable_times = [5.0, 14.0, 28.0, 43.0]
    for index in range(4):
        fight_id = index + 1
        fights.append({'id': fight_id, 'encounterID': 777, 'name': 'Timer Boss',
                       'startTime': 100000 * index, 'endTime': 100000 * index + 70000,
                       'kill': index == 3, 'difficulty': 16, 'size': 5})
        rows = []
        for ability_id, times, amount in (
                (100, high_times[index], 200),
                (101, (25.0,), 40),
                (102, (unstable_times[index],), 200)):
            for occurrence in times:
                for player_id in range(1, 5):
                    rows.append({'timestamp': fights[-1]['startTime'] + occurrence * 1000,
                                 'type': 'damage', 'sourceID': 99, 'targetID': player_id,
                                 'abilityGameID': ability_id, 'amount': amount,
                                 'targetResources': {'hitPoints': 800,
                                                     'maxHitPoints': 1000}})
        rows.append({'timestamp': fights[-1]['startTime'] + (30 + index * .1) * 1000,
                     'type': 'begincast', 'sourceID': 99, 'targetID': 0,
                     'abilityGameID': 103})
        events[str(fight_id)] = rows
    # A short wipe did not live long enough to observe the accepted ability.
    fights.append({'id': 5, 'encounterID': 777, 'name': 'Timer Boss',
                   'startTime': 500000, 'endTime': 510000, 'kill': False,
                   'difficulty': 16, 'size': 5})
    events['5'] = []
    report = {'code': 'TimerRpt1', 'title': 'Timer fixture', 'fights': fights,
              'masterData': {'actors': actors, 'abilities': [
                  {'gameID': 100, 'name': 'Raid Blast'},
                  {'gameID': 101, 'name': 'Weak Pulse'},
                  {'gameID': 102, 'name': 'Random Blast'},
                  {'gameID': 103, 'name': 'Phase Shift'}]}}
    path.write_text(json.dumps({'format': 'wow-log-tools-warcraft-logs-v1',
                                'report': report, 'eventsByFight': events}), encoding='utf-8')


def fixture_guide():
    return {
        'format': 'wow-guide-profile/v1',
        'schema_version': 1,
        'profile_id': 'timer-boss-guide-v1',
        'title': 'Timer Boss timed mechanics',
        'source': {'kind': 'test', 'title': 'Test guide', 'content_sha256': 'a' * 64},
        'encounter': {'id': 777, 'name': 'Timer Boss', 'aliases': [],
                      'actors': ['Timer Boss']},
        'timing_policy': {
            'encounter_schedule_source': 'observed_logs_only',
            'effect_intervals_are_cast_intervals': False,
            'guide_context_can_enable_timer': False,
            'damage_values_included': False,
        },
        'phases': [{'id': 'phase_one', 'name': 'Phase One', 'order': 1,
                    'actors': ['Timer Boss']}],
        'mechanics': [
            {
                'id': 'raid-blast', 'name': 'Raid Blast', 'actors': ['Timer Boss'],
                'phases': ['phase_one'],
                'difficulties': ['mythic'],
                'categories': ['raidwide'], 'severity': 'critical',
                'facts': {'effect_duration_seconds': 5},
                'timer_policy': {'mode': 'observed_static', 'candidate': True,
                                 'reason': 'Use observed pull timing.'},
            },
            {
                'id': 'phase-shift', 'name': 'Phase Shift', 'actors': ['Timer Boss'],
                'phases': ['phase_one'],
                'difficulties': ['mythic'],
                'categories': ['phase_transition'], 'severity': 'critical',
                'facts': {},
                'timer_policy': {'mode': 'phase', 'candidate': True,
                                 'reason': 'Use observed cast timing.'},
            },
        ],
    }


class MechanicTimerLearnerTests(unittest.TestCase):
    def test_guide_context_annotates_without_adding_damage_to_timer(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            cache = Path(folder) / 'warcraft-logs-TimerRpt1.wcl.json'
            fixture_report(cache)

            result = learn_timer_script([cache], 777, 16, guide_profile=fixture_guide())
            export = build_timer_export(result)

            accepted = result['accepted_abilities'][0]
            self.assertEqual(accepted['guide_context']['mechanic_id'], 'raid-blast')
            phase = next(item for item in result['accepted_abilities']
                         if item['ability'] == 'Phase Shift')
            self.assertEqual(phase['observation_kind'], 'boss_cast_start')
            self.assertNotIn('median_target_health_fraction', phase['evidence'])
            self.assertEqual(result['guide_context']['matched_candidate_count'], 2)
            self.assertEqual(export['provenance']['guide_context']['profile_id'],
                             'timer-boss-guide-v1')
            self.assertFalse(any('damage' in key.casefold()
                                 for item in export['callouts'] for key in item))

    def test_conservative_learning_and_partial_wipe_handling(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            cache = Path(folder) / 'warcraft-logs-TimerRpt1.wcl.json'
            fixture_report(cache)
            result = learn_timer_script([cache], 777, 16)
            self.assertEqual([item['fight_id'] for item in result['source_fights']],
                             [1, 2, 3, 4, 5])
            self.assertEqual([item['ability'] for item in result['accepted_abilities']],
                             ['Raid Blast'])
            rejected = {item['ability']: item['reasons'] for item in result['rejected_abilities']}
            self.assertIn('damage_below_health_fraction_threshold', rejected['Weak Pulse'])
            self.assertIn('inconsistent_first_occurrence', rejected['Random Blast'])
            accepted = result['accepted_abilities'][0]
            self.assertEqual(accepted['evidence']['eligible_pulls'], 4)
            self.assertEqual(len(accepted['evidence']['predicted_occurrences']), 2)
            script = result['script']
            self.assertTrue(script['draft'])
            self.assertFalse(script['enabled'])
            self.assertEqual(script['encounter_triggers']['start']['event_type'],
                             'ENCOUNTER_START')
            self.assertTrue(script['encounter_triggers']['start']['establishes_time_zero'])
            self.assertEqual(script['encounter_triggers']['end']['event_type'],
                             'ENCOUNTER_END')
            self.assertTrue(script['encounter_triggers']['end']['cancel_pending_mechanics'])
            self.assertEqual(
                script['encounter_triggers']['end']['evidence']['end_boundaries_observed'], 5)
            self.assertIsNotNone(result['source_fights'][0]['start_time_ms'])
            self.assertIsNotNone(result['source_fights'][0]['end_time_ms'])
            self.assertEqual(script['mechanics'][0]['kind'], 'WARNING')
            self.assertAlmostEqual(script['mechanics'][0]['time'], 15.1)
            with self.assertRaisesRegex(ValueError, 'disabled draft'):
                MechanicScriptRegistry([script])

    def test_draft_storage_is_managed_and_path_safe(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            cache = root / 'warcraft-logs-TimerRpt1.wcl.json'
            fixture_report(cache)
            result = learn_timer_script([cache], 777, 16)
            destination = root / 'mechanic-drafts' / f"{result['draft_id']}.json"
            save_draft(result, destination)
            service = AnalysisService(root)
            self.assertEqual(len(service.list_mechanic_drafts()['drafts']), 1)
            self.assertEqual(service.get_mechanic_draft(result['draft_id'])['encounter_id'], 777)
            with self.assertRaises(ServiceError) as raised:
                service.get_mechanic_draft('../secret')
            self.assertEqual(raised.exception.code, 'INVALID_DRAFT_ID')

    def test_requires_at_least_three_pulls(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            cache = Path(folder) / 'warcraft-logs-TimerRpt1.wcl.json'
            fixture_report(cache)
            with self.assertRaisesRegex(ValueError, 'At least 3'):
                learn_timer_script([cache], 777, 16, fight_ids=[1, 2])

    def test_multiple_reports_are_interpreted_in_parallel_and_merged(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            first = root / 'warcraft-logs-TimerRpt1.wcl.json'
            second = root / 'warcraft-logs-TimerRpt2.wcl.json'
            fixture_report(first)
            fixture_report(second)
            payload = json.loads(second.read_text(encoding='utf-8'))
            payload['report']['code'] = 'TimerRpt2'
            second.write_text(json.dumps(payload), encoding='utf-8')
            messages = []

            result = learn_timer_script(
                [first, second], 777, 16, max_workers=2, progress=messages.append)

            self.assertEqual(result['report_ids'], ['TimerRpt1', 'TimerRpt2'])
            self.assertEqual(result['pull_count'], 10)
            self.assertIn(result['analysis']['mode'],
                          ('parallel_processes', 'parallel_threads'))
            self.assertEqual(result['analysis']['worker_count'], 2)
            self.assertEqual(len(result['source_fights']), 10)
            self.assertTrue(any('2 worker processes' in message for message in messages))

    def test_cross_report_learning_rejects_ambiguous_fight_ids(self):
        with self.assertRaisesRegex(ValueError, 'single report'):
            learn_timer_script(['one.wcl.json', 'two.wcl.json'], 777, 16,
                               fight_ids=[1, 2, 3])


if __name__ == '__main__':
    unittest.main()
