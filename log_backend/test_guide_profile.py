import copy
import unittest
from pathlib import Path

if __package__:
    from .guide_profile import annotate_timer_candidates, load_guide_profile, validate_guide_profile
else:
    from guide_profile import annotate_timer_candidates, load_guide_profile, validate_guide_profile


PROFILE = Path(__file__).parent / 'guide_profiles' / 'zuljan-malacrass.guide.json'


class GuideProfileTests(unittest.TestCase):
    def test_curated_profile_is_valid_and_timer_focused(self):
        profile = load_guide_profile(PROFILE)

        self.assertEqual(len(profile['phases']), 4)
        self.assertGreaterEqual(len(profile['mechanics']), 30)
        self.assertFalse(profile['timing_policy']['damage_values_included'])
        self.assertTrue(any(item['timer_policy']['candidate']
                            for item in profile['mechanics']))
        self.assertFalse(any(
            key in {'damage', 'damage_amount', 'damage_value', 'amount'}
            for mechanic in profile['mechanics']
            for key in mechanic.get('facts', {})
        ))

    def test_matches_log_ability_name_without_changing_acceptance(self):
        profile = load_guide_profile(PROFILE)
        candidates = [
            {'ability': 'Eternal Nightfall', 'accepted': True},
            {'ability': 'Unknown Spell', 'accepted': False},
        ]

        summary = annotate_timer_candidates(candidates, profile)

        self.assertTrue(candidates[0]['accepted'])
        self.assertEqual(candidates[0]['guide_context']['mechanic_id'],
                         'eternal-nightfall')
        self.assertNotIn('guide_context', candidates[1])
        self.assertEqual(summary['matched_candidate_count'], 1)

    def test_rejects_damage_values_in_guide_facts(self):
        profile = load_guide_profile(PROFILE)
        changed = copy.deepcopy(profile)
        changed['mechanics'][0]['facts']['damage_amount'] = 123

        with self.assertRaisesRegex(ValueError, 'forbidden damage'):
            validate_guide_profile(changed)


if __name__ == '__main__':
    unittest.main()
