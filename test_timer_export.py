import json
import tempfile
import unittest
from pathlib import Path

from mechanic_timeline import MechanicScriptRegistry
from timer_export import build_timer_export, save_timer_export, timer_export_to_script


def sample_draft():
    return {
        'schema_version': 2,
        'draft_id': 'sample-draft',
        'generated_at': '2026-01-01T00:00:00+00:00',
        'status': 'DRAFT_REQUIRES_REVIEW',
        'report_ids': ['Report001'],
        'pull_count': 4,
        'source_fights': [{'report_id': 'Report001', 'fight_id': 2}],
        'analysis': {'mode': 'single_process', 'worker_count': 1},
        'script': {
            'script_id': 'sample-draft',
            'encounter_id': 3429,
            'encounter_name': 'Example Boss',
            'difficulty': 4,
            'enabled': False,
            'encounter_triggers': {
                'start': {
                    'id': 'fight-begin', 'event_type': 'ENCOUNTER_START',
                    'action': 'START_TIMELINE', 'establishes_time_zero': True,
                    'message': 'Engage Example Boss now.',
                },
                'end': {
                    'id': 'fight-end', 'event_type': 'ENCOUNTER_END',
                    'action': 'END_TIMELINE', 'cancel_pending_mechanics': True,
                    'message': 'Example Boss ended: {result}.',
                },
            },
            'mechanics': [
                {'id': 'blast-warning', 'time': 5, 'kind': 'WARNING',
                 'ability_id': 100, 'message': 'Blast expected in 5 seconds.'},
                {'id': 'blast', 'time': 10, 'kind': 'EXPECTED_ACTIVATION',
                 'ability_id': 100, 'message': 'Blast expected now.'},
            ],
        },
    }


class TimerExportTests(unittest.TestCase):
    def test_builds_bot_neutral_engage_and_callout_contract(self):
        payload = build_timer_export(sample_draft())

        self.assertEqual(payload['format'], 'wow-fight-timer/v1')
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['triggers']['start']['emit'], {
            'type': 'FIGHT_ENGAGED',
            'message': 'Engage Example Boss now.',
        })
        self.assertEqual(payload['triggers']['end']['emit']['type'], 'FIGHT_ENDED')
        self.assertTrue(payload['triggers']['end']['cancel_pending_callouts'])
        self.assertEqual(payload['callouts'][0]['offset_seconds'], 5.0)
        self.assertEqual(payload['provenance']['pull_count'], 4)

    def test_saves_atomically_and_rejects_duplicate_callout_ids(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            destination = Path(folder) / 'timer.timer.json'
            payload = build_timer_export(sample_draft())
            save_timer_export(payload, destination)
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8'))['timer_id'],
                             'sample-draft')
            self.assertFalse(destination.with_suffix('.json.part').exists())

        draft = sample_draft()
        draft['script']['mechanics'].append(dict(draft['script']['mechanics'][0]))
        with self.assertRaisesRegex(ValueError, 'unique'):
            build_timer_export(draft)

    def test_enabled_export_loads_as_a_live_timer_module(self):
        enabled = build_timer_export(sample_draft(), enabled=True)
        script = timer_export_to_script(enabled)
        self.assertTrue(enabled['enabled'])
        self.assertEqual(enabled['status'], 'REVIEWED_ENABLED')
        self.assertEqual(script['encounter_id'], 3429)
        self.assertEqual(script['mechanics'][0]['time'], 5.0)

        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            save_timer_export(enabled, root / 'enabled.timer.json')
            save_timer_export(build_timer_export(sample_draft(), enabled=False),
                              root / 'disabled.timer.json')
            registry = MechanicScriptRegistry.from_file(root)
            self.assertEqual([item['script_id'] for item in registry.scripts], ['sample-draft'])
            self.assertEqual(registry.scripts[0]['encounter_triggers']['start']['message'],
                             'Engage Example Boss now.')
            status = registry.arm('sample-draft')
            self.assertEqual(status['armed_script_id'], 'sample-draft')


if __name__ == '__main__':
    unittest.main()
