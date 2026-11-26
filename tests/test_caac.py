"""CAAC plan parsing: counts (equal to the former rebuild_vamsys_plan.py results) and grouping."""

import unittest

from tests import _support as S
from vcca import caac

# Numbers for the 2026-27-1 season files (verified against the former rebuild_vamsys_plan.py, 2026-11-25).
LEGACY_RAW_ROWS = 3475
LEGACY_LEGS = 3571
LEGACY_NUMBERS = 2087
LEGACY_ROTATION_GAPS = 29
FX = S.FIXTURE["plan"]          # the fixture season (HK/Macau/Taiwan file only), see tools/make_fixtures.py


@S.full_data
@unittest.skipUnless(S.SEASON == "2026-27-1", "legacy counts are for season 2026-27-1")
class CaacCountsTest(unittest.TestCase):
    def test_counts(self):
        p = S.plan()
        self.assertEqual(len(p.rows), LEGACY_RAW_ROWS)
        self.assertEqual(len(p.legs), LEGACY_LEGS)
        self.assertEqual(len(p.numbers), LEGACY_NUMBERS)
        self.assertEqual(len(caac.detect_rotation_gaps(p.legs, S.cfg().number_prefix)), LEGACY_ROTATION_GAPS)
        self.assertEqual((p.fd23_mode, p.fd24_mode), ("1025", "0327"))

    def test_rotation_gap_examples(self):
        gaps = caac.detect_rotation_gaps(S.plan().legs)
        self.assertIn(("CAO", "CAO3155", "ESSA", "EBLG"), gaps)

    def test_real_season_groups(self):
        groups = caac.group_legs(S.plan().legs, dict(S.cfg().airport_alias))
        self.assertEqual(sum(len(g.legs) for g in groups.values()), LEGACY_LEGS)
        g = groups[("CCA", "CCA1109", "ZBAA", "ZBXH")]
        # micro-split in the plan: Sun 02:40Z and Mon-Sat 02:55Z stay separate raw slots
        self.assertEqual([(s.dep, s.fd3) for s in g.raw_slots], [("02:40", "......7"), ("02:55", "123456.")])
        self.assertTrue(any(k[1] == "CCA754" for k in groups if groups[k].single_day_legs))


@S.fixture_only
class CaacFixtureCountsTest(unittest.TestCase):
    """Same checks on the fixture season (values recorded by the generator from the real file)."""

    def test_counts(self):
        p = S.plan()
        self.assertEqual((len(p.rows), len(p.legs), len(p.numbers)), (FX["rows"], FX["legs"], FX["numbers"]))
        self.assertEqual(len(caac.detect_rotation_gaps(p.legs, S.cfg().number_prefix)), FX["rotation_gaps"])
        self.assertEqual([p.fd23_mode, p.fd24_mode], FX["fd23_fd24"])
        self.assertEqual((p.fd23_mode, p.fd24_mode), ("1025", "0327"))   # same season as the real data

    def test_micro_split_example(self):
        groups = caac.group_legs(S.plan().legs, dict(S.cfg().airport_alias))
        self.assertEqual(len(groups), FX["groups"])
        g = groups[tuple(FX["micro_split_example"])]
        self.assertEqual([[s.dep, s.fd3] for s in g.raw_slots], FX["micro_split_slots"])
        self.assertGreater(len(g.raw_slots), 1)


class CaacGroupsTest(unittest.TestCase):
    """Grouping invariants: hold for any season data."""

    def test_groups(self):
        legs = S.plan().legs
        groups = caac.group_legs(legs, dict(S.cfg().airport_alias))
        self.assertEqual(sum(len(g.legs) for g in groups.values()), len(legs))
        self.assertTrue(any(len(g.raw_slots) > 1 for g in groups.values()))
        single = [g for g in groups.values() if g.is_single_day_only]
        self.assertTrue(all(l.single_day for g in single for l in g.legs))
        self.assertTrue(all(k == g.key for k, g in groups.items()))


if __name__ == "__main__":
    unittest.main()
