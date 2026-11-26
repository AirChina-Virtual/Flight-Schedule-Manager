"""rules/*.toml load and behave as expected (they were verified equal to the former Python constants)."""

import unittest

from tests import _support as S  # noqa: F401  (sys.path)
from vcca.config import load_airport_rules, load_fleet_rules


class AirportRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = load_airport_rules()

    def test_unrated_flagged(self):
        self.assertEqual(self.r.route_min_grade("ZBAA", "XXXX"), ("4F", "4C", "4C", ("XXXX",)))
        self.assertIsNone(self.r.grade("XXXX"))
        self.assertEqual(self.r.grade_or_fallback("zbaa"), "4F")

    def test_groups_expanded(self):
        self.assertTrue(self.r.fleet_groups["high_plateau_excluded"])
        for ap, ex in self.r.fleet_exclusions.items():
            self.assertTrue(all(not x.startswith("@") for x in ex), ap)


class FleetRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = load_fleet_rules()

    def test_grade_pools_nested(self):
        nb, wb = self.f.cca_narrowbody, self.f.cca_widebody
        self.assertLessEqual(nb["3C"], nb["4C"])
        self.assertLessEqual(nb["4C"], nb["4D"])
        self.assertLessEqual(wb["4D"], wb["4E"])
        self.assertLessEqual(wb["4E"], wb["4F"])
        self.assertEqual(self.f.nb_all, nb["4D"])
        self.assertEqual(self.f.wb_all, wb["4F"])

    def test_map_ac_to_fleet(self):
        for ac, fid in self.f.ac_to_fleet_pax.items():
            self.assertEqual(self.f.map_ac_to_fleet("CCA", ac.lower(), cargo=False), fid)
        for ac, fid in self.f.ac_to_fleet_cargo.items():
            self.assertEqual(self.f.map_ac_to_fleet("CAO", ac, cargo=True), fid)
        self.assertIsNone(self.f.map_ac_to_fleet("CCA", "XYZ", cargo=False))


if __name__ == "__main__":
    unittest.main()
