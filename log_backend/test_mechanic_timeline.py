from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

if __package__:
    from .analysis_service import AnalysisService
    from .live_log_service import LiveLogService
    from .mechanic_timeline import (EncounterTimelineEngine, MechanicScriptRegistry,
                                    TimelineEncounter)
    from .test_live_log_service import BOSS_HIT, END, START, line
else:
    from analysis_service import AnalysisService
    from live_log_service import LiveLogService
    from mechanic_timeline import (EncounterTimelineEngine, MechanicScriptRegistry,
                                   TimelineEncounter)
    from test_live_log_service import BOSS_HIT, END, START, line


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def utc_now(self):
        return datetime.fromtimestamp(self.now, timezone.utc).isoformat()

    def advance(self, seconds):
        self.now += seconds


def registry():
    return MechanicScriptRegistry([{
        'id': 'test-placeholder', 'encounterId': 1, 'difficulty': 'any',
        'placeholder': True, 'mechanics': [
            {'id': 'pull', 'time': 0, 'message': 'Started.'},
            {'id': 'warning', 'time': 10, 'message': 'Warning.'},
            {'id': 'later', 'time': 20, 'message': 'Later.'},
        ],
    }])


class MechanicTimelineTests(unittest.TestCase):
    def test_prearmed_module_is_verified_against_live_difficulty(self):
        scripts = [{
            'script_id': timer_id, 'encounter_id': 7, 'encounter_name': 'Boss',
            'difficulty': difficulty, 'placeholder': False,
            'mechanics': [{'id': 'call', 'time': 1, 'message': timer_id}],
        } for timer_id, difficulty in (('heroic', 15), ('mythic', 16))]
        selected = MechanicScriptRegistry(scripts)
        selected.arm('mythic', reload=False)
        engine = EncounterTimelineEngine(selected, FakeClock())

        engine.start(TimelineEncounter('s', 'a', 1, 7, 'Boss', 16, 20, 1000))

        self.assertEqual(engine.snapshot()['script_id'], 'mythic')
        self.assertEqual(selected.status()['armed_script_id'], 'mythic')
        # A different live difficulty cannot load the armed Mythic module.
        self.assertEqual(selected.select(7, 15)['script_id'], 'heroic')

    def test_fake_clock_trigger_cancel_and_reset(self):
        clock = FakeClock()
        engine = EncounterTimelineEngine(registry(), clock)
        encounter = TimelineEncounter('session', 'attempt-1', 1, 1, 'Boss', 15, 20, 1000)
        emitted = engine.start(encounter)
        self.assertEqual([event['type'] for event in emitted],
                         ['ENCOUNTER_STARTED', 'MECHANIC_EVENT'])
        self.assertEqual(emitted[0]['trigger']['event_type'], 'ENCOUNTER_START')
        self.assertTrue(emitted[0]['trigger']['establishes_time_zero'])
        clock.advance(9)
        self.assertEqual(engine.tick(), [])
        clock.advance(1)
        self.assertEqual(engine.tick()[0]['mechanic_id'], 'warning')
        ended = engine.end(False, 1012)
        self.assertEqual(ended[-1]['result'], 'WIPE')
        self.assertEqual(ended[-1]['trigger']['event_type'], 'ENCOUNTER_END')
        self.assertTrue(ended[-1]['trigger']['cancel_pending_mechanics'])
        self.assertEqual(ended[-2]['status'], 'CANCELED')
        second = TimelineEncounter('session', 'attempt-2', 2, 1, 'Boss', 15, 20, 2000)
        engine.start(second)
        self.assertEqual(engine.snapshot()['events'][0]['attempt_id'], 'attempt-2')

    def test_live_monitor_emits_structured_timeline(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            log = root / 'WoWCombatLog.txt'
            log.write_text(BOSS_HIT, encoding='utf-8')
            clock = FakeClock()
            live = LiveLogService(log, root, from_end=False, rebuild_interval=.01,
                                  mechanic_registry=registry(), timeline_clock=clock)
            service = AnalysisService(root, live)
            live.scan_once()
            self.assertEqual(live.timeline_snapshot()['state'], 'IDLE')
            with log.open('a', encoding='utf-8') as output:
                output.write(START + START)
            live.scan_once()
            self.assertEqual(service.get_live_timeline()['script_id'], 'test-placeholder')
            started = [event for event in live.timeline_snapshot()['events']
                       if event['type'] == 'ENCOUNTER_STARTED']
            self.assertEqual(len(started), 1)
            clock.advance(10)
            live.scan_once()
            triggered = service.get_live_mechanic_events('TRIGGERED')['events']
            self.assertEqual([event['mechanic_id'] for event in triggered], ['pull', 'warning'])
            with log.open('a', encoding='utf-8') as output:
                output.write(END)
            live.scan_once()
            timeline = service.get_live_timeline()
            self.assertEqual(timeline['result'], 'WIPE')
            self.assertEqual(timeline['events'][-1]['type'], 'ENCOUNTER_ENDED')
            self.assertEqual(service.get_live_mechanic_events('CANCELED')['events'][0][
                'mechanic_id'], 'later')
            self.assertEqual(service.list_live_attempts()['attempts'][0]['result'], 'WIPE')
            self.assertEqual(service.get_live_attempt_timeline(1)['events'][-1]['type'],
                             'ENCOUNTER_ENDED')
            live.stop()

    def test_new_pull_interrupts_old_and_kill_finishes_new_attempt(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            log = root / 'WoWCombatLog.txt'
            log.write_text(START, encoding='utf-8')
            clock = FakeClock()
            live = LiveLogService(log, root, from_end=False, rebuild_interval=.01,
                                  mechanic_registry=registry(), timeline_clock=clock)
            live.scan_once()
            second_start = line('05.000', ['ENCOUNTER_START', 1, 'Test Boss', 15, 2, 999])
            kill = line('08.000', ['ENCOUNTER_END', 1, 'Test Boss', 15, 2, 1, 3000])
            with log.open('a', encoding='utf-8') as output:
                output.write(second_start + kill)
            live.scan_once()
            self.assertEqual(live.timeline_snapshot()['result'], 'KILL')
            self.assertEqual(live.timeline_snapshot()['events'][0]['attempt'], 2)
            live.stop()


if __name__ == '__main__':
    unittest.main()
