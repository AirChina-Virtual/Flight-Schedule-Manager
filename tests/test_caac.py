"""CAAC plan parsing: counts and field-by-field parity with legacy rebuild_vamsys_plan.load_raw_preplan."""

import unittest

from tests import _support as S
from vcca import caac

# Legacy numbers for the 2026-27-1 season files (rebuild_vamsys_plan.py, 2026-11-25).
LEGACY_RAW_ROWS = 3475
LEGACY_LEGS = 3571
LEGACY_NUMBERS = 2087
LEGACY_ROTATION_GAPS = 29


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

    def test_groups(self):
        groups = caac.group_legs(S.plan().legs, dict(S.cfg().airport_alias))
        self.assertEqual(sum(len(g.legs) for g in groups.values()), LEGACY_LEGS)
        g = groups[("CCA", "CCA1109", "ZBAA", "ZBXH")]
        # micro-split in the plan: Sun 02:40Z and Mon-Sat 02:55Z stay separate raw slots
        self.assertEqual([(s.dep, s.fd3) for s in g.raw_slots], [("02:40", "......7"), ("02:55", "123456.")])
        single = [g for g in groups.values() if g.is_single_day_only]
        self.assertTrue(all(l.single_day for g in single for l in g.legs))
        self.assertTrue(any(k[1] == "CCA754" for k in groups if groups[k].single_day_legs))


@unittest.skipUnless(S.SEASON == "2026-27-1", "legacy parity is for season 2026-27-1")
class CaacLegacyParityTest(unittest.TestCase):
    def test_raw_rows_identical(self):
        legacy_rows, _ = S.legacy_plan()
        self.assertEqual(S.plan().rows, legacy_rows)

    def test_legs_field_by_field(self):
        _, legacy_legs = S.legacy_plan()
        new = [caac.legacy_leg_dict(l) for l in S.plan().legs]
        self.assertEqual(len(new), len(legacy_legs))
        diffs = []
        for i, (a, b) in enumerate(zip(new, legacy_legs)):
            if a != b:
                keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                diffs.append((i, b.get("src_ref"), {k: (a.get(k), b.get(k)) for k in keys}))
        self.assertEqual(diffs[:5], [], f"{len(diffs)} differing legs")

    def test_rotation_gaps_identical(self):
        _, legacy_legs = S.legacy_plan()
        legacy = S.legacy_rebuild().detect_rotation_gaps(legacy_legs)
        new = caac.detect_rotation_gaps(S.plan().legs)
        self.assertEqual(set(new), set(legacy))
        for k, g in new.items():
            self.assertEqual((g.out_callsign, g.ret_callsign, g.route_type),
                             (legacy[k]["out_cs"], legacy[k]["ret_cs"], legacy[k]["route_type"]))


if __name__ == "__main__":
    unittest.main()
