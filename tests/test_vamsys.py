"""VAMSYS read-only loader: counts and parity with legacy load_vamsys_db."""

import unittest

from tests import _support as S
from vcca import vamsys


class VamsysLoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vs = vamsys.load_vamsys(S.cfg())

    def test_counts(self):
        c = self.vs.counts()
        self.assertEqual(c["airports"], 243)
        self.assertEqual(c["fleets"], 37)
        self.assertEqual(c["aircraft"], 586)
        self.assertEqual(c["routings"], 15)
        # Route counts change whenever FlightOpsDesk syncs; check internal consistency + the known lower bound.
        self.assertGreater(c["routes"], 2000)
        self.assertEqual(c["routes"], c["commercial_routes"] + c["special_routes"])
        self.assertEqual(sum(c["routes_by_op"].values()), c["routes"])
        self.assertIsNotNone(self.vs.revision)

    def test_parity_with_legacy(self):
        rev, airports, fleets, aircraft, routes = S.legacy_rebuild().load_vamsys_db()
        vs = self.vs
        self.assertEqual(rev, vs.revision)
        self.assertEqual(set(airports), set(vs.airports))
        self.assertEqual(set(fleets), set(vs.fleets))
        self.assertEqual(len(aircraft), len(vs.aircraft))
        self.assertEqual(len(routes), len(vs.routes))
        by_id = vs.route_by_id()
        for f in routes:
            r = by_id[f["ID"]]
            self.assertEqual((r.op, r.callsign, r.number, r.orig, r.dest),
                             (f["_op"], f["_cs"], f["_fn"], f["_orig"], f["_dest"]))
            self.assertEqual((r.dep_min, r.arr_min, r.dep, r.arr), (f["_dep_min"], f["_arr_min"], f["_dep_hm"], f["_arr_hm"]))
            self.assertEqual(r.days, frozenset(f["_days"]))
            self.assertEqual(r.fleet_ids, frozenset(f["_fids"]))
            self.assertEqual(r.is_special, f["_is_special"])

    def test_route_fields(self):
        r = self.vs.routes[0]
        self.assertTrue(r.id.isdigit())
        self.assertIn(r.type, ("scheduled", "cargo", "training", "repositioning", "charter"))
        for r in self.vs.commercial_routes:
            self.assertIn(r.op, S.cfg().target_ops)
        keys = self.vs.routes_by_key()
        self.assertEqual(sum(len(v) for v in keys.values()), len(self.vs.commercial_routes))

    def test_readonly(self):
        conn = vamsys.connect_ro(S.cfg().vamsys_db)
        try:
            with self.assertRaises(Exception):
                conn.execute("CREATE TABLE _probe(x)")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
