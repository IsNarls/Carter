import tempfile
import unittest
from pathlib import Path

if __package__:
    from .combat_log_viewer import parse_event
    from .encounter_parser import (absorb_from_event, analyze_encounter, damage_from_event,
                                   healing_from_event, scan_encounters,
                                   unit_snapshot, write_boss_position_csv, write_csv,
                                   write_player_csv)
else:
    from combat_log_viewer import parse_event
    from encounter_parser import (absorb_from_event, analyze_encounter, damage_from_event,
                                  healing_from_event, scan_encounters,
                                  unit_snapshot, write_boss_position_csv, write_csv,
                                  write_player_csv)


START = '4/8/2026 20:00:00.000-7  ENCOUNTER_START,1,"Test Boss",15,20,999\n'
CAST_START = ('4/8/2026 20:00:00.250-7  SPELL_CAST_START,Creature-1,"Test Boss",0,0,'
              'Player-1,"Hero",0,0,999,"Big Blast",4\n')
CAST_SUCCESS = ('4/8/2026 20:00:00.750-7  SPELL_CAST_SUCCESS,Creature-1,"Test Boss",0,0,'
                'Player-1,"Hero",0,0,999,"Big Blast",4\n')
SPELL = ('4/8/2026 20:00:01.000-7  SPELL_DAMAGE,Player-1,"Hero",0x514,0x0,'
         'Creature-1,"Test Boss",0xa48,0x0,123,"Bolt",0x1,Creature-1,0,'
         '900,1000,0,0,0,0,0,0,0,0,100,0,1,2,3,4,5,100,10,-1,1,0,0,0,nil,nil,nil,ST\n')
SWING = ('4/8/2026 20:00:02.000-7  SWING_DAMAGE,Player-1,"Hero",0x514,0x0,'
         'Creature-1,"Test Boss",0xa48,0x0,Player-1,0,500,500,0,0,0,0,0,0,0,'
         '0,100,0,1,2,3,4,5,50,0,-1,1,0,0,0,nil,nil,nil\n')
LANDED = ('4/8/2026 20:00:02.000-7  SWING_DAMAGE_LANDED,Player-1,"Hero",0x514,0x0,'
          'Creature-1,"Test Boss",0xa48,0x0,Creature-1,0,850,1000,0,0,0,0,0,0,0,'
          '0,100,0,1,2,3,4,5,50,0,-1,1,0,0,0,nil,nil,nil\n')
HEAL = ('4/8/2026 20:00:01.500-7  SPELL_HEAL,Player-1,"Hero",0x514,0x0,'
        'Player-1,"Hero",0x514,0x0,456,"Heal",0x2,Player-1,0,500,500,0,0,0,0,0,'
        '0,0,0,100,0,1,2,3,4,5,75,100,0,25,nil\n')
END = '4/8/2026 20:00:03.000-7  ENCOUNTER_END,1,"Test Boss",15,20,1,3000\n'
ABSORB = ('4/8/2026 20:00:01.750-7  SPELL_ABSORBED,Creature-1,"Test Boss",0,0,'
          'Player-1,"Hero",0,0,Player-1,"Hero",0,0,17,"Shield",2,30,100,nil\n')


class EncounterParserTests(unittest.TestCase):
    def test_damage_snapshot_offsets(self):
        spell = damage_from_event(parse_event(SPELL.rstrip()))
        self.assertEqual((spell.current_health, spell.max_health), (900, 1000))
        self.assertEqual((spell.amount, spell.effective), (100, 100))
        position = unit_snapshot(parse_event(SWING.rstrip()))
        self.assertIsNotNone(position)
        self.assertEqual(healing_from_event(parse_event(HEAL.rstrip())), 75)
        self.assertEqual(absorb_from_event(parse_event(ABSORB.rstrip()))[2], 30)

    def test_timeline_counts_swing_once_and_uses_landed_health(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            log = Path(folder) / 'log.txt'
            log.write_text(START + CAST_START + CAST_SUCCESS + SPELL + HEAL + ABSORB +
                           SWING + LANDED + END, encoding='utf-8')
            refs = scan_encounters(log)
            self.assertEqual(len(refs), 1)
            timeline = analyze_encounter(log, refs[0])
            self.assertTrue(refs[0].success)
            self.assertEqual(timeline.bosses[0].damage, 150)
            self.assertEqual(timeline.rows[1].health, 900)
            self.assertEqual(timeline.rows[2].health, 850)
            self.assertEqual(timeline.rows[2].damage, 50)
            self.assertEqual(len(timeline.boss_profiles), 1)
            self.assertTrue(timeline.boss_profiles[0].positions)
            self.assertIn(timeline.bosses[0].guid, timeline.rows[1].boss_positions)
            self.assertEqual(len(timeline.mechanics), 1)
            self.assertEqual(timeline.mechanics[0].ability, 'Big Blast')
            self.assertAlmostEqual(timeline.mechanics[0].end - timeline.mechanics[0].start, 0.5)
            self.assertEqual(len(timeline.players), 1)
            self.assertEqual(timeline.players[0].damage, 150)
            self.assertEqual(timeline.players[0].healing, 105)
            self.assertEqual(timeline.players[0].hps, 35)
            self.assertEqual(timeline.rows[1].player_healing[timeline.players[0].guid], 105)
            self.assertGreater(timeline.players[0].position_samples, 0)
            output = Path(folder) / 'timeline.csv'
            write_csv(timeline, output)
            self.assertIn('Test Boss health pct', output.read_text(encoding='utf-8-sig'))
            player_output = Path(folder) / 'players.csv'
            write_player_csv(timeline, player_output)
            player_text = player_output.read_text(encoding='utf-8-sig')
            self.assertIn('fight_dps', player_text)
            self.assertIn('fight_hps', player_text)
            self.assertIn('Hero', player_text)
            boss_output = Path(folder) / 'bosses.csv'
            write_boss_position_csv(timeline, boss_output)
            self.assertIn('boss_guid', boss_output.read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
