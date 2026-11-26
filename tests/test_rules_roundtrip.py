"""rules/*.toml must equal the legacy Python constants exactly (and behave identically)."""

import unittest

from tests import _support as S
from vcca.config import load_airport_rules, load_fleet_rules


class AirportRulesRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import airport_grades as AG  # original fr24_work/airport_grades.py

        cls.AG = AG
        cls.r = load_airport_rules()

    def test_tables(self):
        AG, r = self.AG, self.r
        self.assertEqual(dict(r.grade_rank), AG.GRADE_RANK)
        self.assertEqual(dict(r.airport_grade), AG.AIRPORT_GRADE)
        self.assertEqual(set(r.high_plateau), AG.HIGH_PLATEAU_AIRPORTS)
        self.assertEqual(set(r.historic_b757), AG.HISTORIC_B757_HIGH_PLATEAU_AIRPORTS)
        self.assertEqual({k: set(v) for k, v in r.fleet_exclusions.items()}, AG.AIRPORT_FLEET_EXCLUSIONS)
        self.assertEqual(set(r.fleet_groups["high_plateau_excluded"]), AG._HIGH_PLATEAU_EXCLUDED)
        self.assertEqual(set(r.fleet_groups["non_c909_pax"]), AG._NON_C909_PAX)

    def test_functions_equivalent(self):
        AG, r = self.AG, self.r
        aps = sorted(AG.AIRPORT_GRADE) + ["XXXX", "", "zbaa"]
        for a in aps:
            self.assertEqual(r.grade_or_fallback(a), AG.get_airport_grade(a), a)
            self.assertEqual(r.airport_allows_b752(a), AG.airport_allows_b752(a), a)
        sample = aps[::7] + ["ZUBD", "ZULS", "ZUNZ", "XXXX"]
        for o in sample:
            for d in sample:
                self.assertEqual(r.route_min_grade(o, d)[:3], AG.route_min_grade(o, d))
                self.assertEqual(r.route_allows_b752(o, d), AG.route_allows_b752(o, d))
                self.assertEqual(set(r.route_fleet_exclusions(o, d)), AG.get_route_fleet_exclusions(o, d))

    def test_unrated_flagged(self):
        self.assertEqual(self.r.route_min_grade("ZBAA", "XXXX"), ("4F", "4C", "4C", ("XXXX",)))
        self.assertIsNone(self.r.grade("XXXX"))


class FleetRulesRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = S.legacy_rebuild()
        cls.f = load_fleet_rules()

    def test_groups(self):
        R, f = self.R, self.f
        self.assertEqual(set(f.cca_narrowbody["3C"]), R.CCA_NB_3C)
        self.assertEqual(set(f.cca_narrowbody["4C"]), R.CCA_NB_4C)
        self.assertEqual(set(f.cca_narrowbody["4D"]), R.CCA_NB_4D)
        self.assertEqual(set(f.cca_widebody["4D"]), R.CCA_WB_4D)
        self.assertEqual(set(f.cca_widebody["4E"]), R.CCA_WB_4E)
        self.assertEqual(set(f.cca_widebody["4F"]), R.CCA_WB_4F)
        self.assertEqual(list(f.fixed_fleets["CAO"]), R.CAO_ACTIVE)
        self.assertEqual(list(f.fixed_fleets["CCD"]), R.CCD_ACTIVE)
        self.assertEqual(list(f.fixed_fleets["BJN"]), R.BJN_ACTIVE)
        self.assertEqual(set(f.dirty), R.EXCLUDED_DIRTY_FLEETS)
        self.assertEqual(set(f.air_force), R.AIR_FORCE_FLEETS)
        self.assertEqual(set(f.nb_all), R.NB_SET)
        self.assertEqual(set(f.wb_all), R.WB_SET)
        self.assertEqual(set(f.cargo_ac_codes), R.CARGO_AC_CODES)
        self.assertEqual(dict(f.ac_to_fleet_pax), R.AC_TO_FLEET_PAX)
        self.assertEqual(dict(f.ac_to_fleet_cargo), R.AC_TO_FLEET_CARGO)

    def test_map_ac_to_fleet_equivalent(self):
        R, cfg = self.R, S.cfg()
        codes = set(R.AC_TO_FLEET_PAX) | set(R.AC_TO_FLEET_CARGO) | {"", "XYZ", "b738", " 73c "}
        for op in ("CCA", "CAO", "CCD", "BJN"):
            for ac in codes:
                self.assertEqual(cfg.map_ac_to_fleet(op, ac), R.map_ac_to_fleet(op, ac), (op, ac))


if __name__ == "__main__":
    unittest.main()
