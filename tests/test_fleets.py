"""fleets: body tier + grade pools + exclusions (plan 5.7),
unrated airports, plateau exclusions, widebody downgrade, audit."""

import unittest

from tests import _support as S
from vcca import fleets as F
from vcca.models import BodyTier, ReasonCode as RC

NB4C = frozenset({"26102", "26071", "26072", "26073", "26074", "26076", "26084", "26085", "26086", "26097"})
PLATEAU_X = {"26074", "26076", "26102"}


def codes(rec):
    return {r.code for r in rec.reasons}


class RecommendTest(unittest.TestCase):
    cfg = S.cfg()

    def rec(self, op, o, d, rtype="scheduled", fd2=(), fr=(), v=()):
        return F.recommend_fleets(self.cfg, op, o, d, route_type=rtype, plan_fd2=fd2, fr24_fleet_ids=fr,
                                  vamsys_fleet_ids=v)

    def test_narrow_4c_and_b752(self):
        ar = self.cfg.airport_rules
        r = self.rec("CCA", "ZBAA", "ZSSS", fd2=["320"])
        self.assertEqual(r.body_tier, BodyTier.NARROW)
        self.assertEqual(r.fleet_ids, self.cfg.fleet_rules.cca_narrowbody["4D"] - ar.route_fleet_exclusions("ZBAA", "ZSSS"))
        self.assertIn("26090", r.fleet_ids)                              # both ends >= 4D -> B752
        self.assertFalse(r.needs_review)

    def test_high_plateau_exclusions(self):
        r = self.rec("CCA", "ZUUU", "ZULS", fd2=["319"])
        self.assertFalse(r.fleet_ids & PLATEAU_X)
        self.assertTrue(r.excluded & PLATEAU_X)
        self.assertIn("26090", r.fleet_ids)                              # historic B757 plateau airport
        w = self.rec("CCA", "ZUUU", "ZULS", fd2=["330"])
        self.assertFalse(w.fleet_ids & {"26088", "26094", "26080", "26594"})   # ZULS widebody stand limits

    def test_widebody_4d_only_and_downgrade(self):
        ar = self.cfg.airport_rules
        g4d = next(a for a, g in sorted(ar.airport_grade.items()) if g == "4D" and not ar.fleet_exclusions.get(a))
        r = self.rec("CCA", "ZBAA", g4d, fd2=["333"])
        self.assertEqual((r.body_tier, r.fleet_ids), (BodyTier.WIDE, frozenset({"26091", "26092"})))
        self.assertIn(RC.FLEET_WB_4D_ONLY, codes(r))
        g4c = next(a for a, g in sorted(ar.airport_grade.items()) if g == "4C" and not ar.fleet_exclusions.get(a))
        d = self.rec("CCA", "ZBAA", g4c, fd2=["333"])
        self.assertEqual(d.fleet_ids, NB4C)
        self.assertIn(RC.FLEET_WB_DOWNGRADED, codes(d))
        self.assertTrue(d.needs_review)
        m = self.rec("CCA", "ZBAA", g4c, fd2=["333", "320"])           # mixed below 4D: narrow pool, noted
        self.assertEqual(m.body_tier, BodyTier.MIXED)
        self.assertEqual(m.fleet_ids, NB4C)

    def test_3c(self):
        ar = self.cfg.airport_rules
        g3c = next((a for a, g in sorted(ar.airport_grade.items()) if g == "3C"), None)
        if g3c is None:
            self.skipTest("no 3C airport in rules")
        r = self.rec("CCA", "ZBAA", g3c, fd2=["320"])
        self.assertEqual(r.fleet_ids, self.cfg.fleet_rules.cca_narrowbody["3C"] - ar.route_fleet_exclusions("ZBAA", g3c))

    def test_unrated_airport_flagged(self):
        r = self.rec("CCA", "ZBAA", "XXXX", fd2=["320"])
        self.assertEqual(r.unrated_airports, ("XXXX",))
        self.assertIn(RC.UNRATED_AIRPORT, codes(r))
        self.assertTrue(r.needs_review)
        self.assertEqual(r.fleet_ids, NB4C)                              # 4C fallback pool (no B752)

    def test_fixed_and_cargo(self):
        fx = self.cfg.fleet_rules.fixed_fleets
        self.assertEqual(self.rec("CAO", "ZSPD", "KORD").fleet_ids, frozenset(fx["CAO"]))
        self.assertEqual(self.rec("CCA", "ZSPD", "KORD", rtype="cargo").fleet_ids, frozenset(fx["CAO"]))
        r = self.rec("BJN", "ZBAA", "ZSSS")
        self.assertEqual((r.fleet_ids, r.body_tier), (frozenset(fx["BJN"]), BodyTier.FIXED))
        ar = self.cfg.airport_rules
        g4c = next(a for a, g in sorted(ar.airport_grade.items()) if g == "4C")
        self.assertIn(RC.FLEET_GRADE_WARNING, codes(self.rec("CAO", "ZSPD", g4c)))

    def test_tier_sources(self):
        self.assertEqual(self.rec("CCA", "ZBAA", "ZSSS", fr=["26077"]).body_tier, BodyTier.WIDE)   # FR24 only
        r = self.rec("CCA", "ZBAA", "ZSSS", v=["26077", "26728"])                                   # VAMSYS fallback
        self.assertEqual(r.body_tier, BodyTier.WIDE)
        self.assertIn(RC.FLEET_FROM_VAMSYS, codes(r))
        n = self.rec("CCA", "ZBAA", "ZSSS", v=["26728"])                  # only dirty -> nothing -> narrow + review
        self.assertEqual(n.body_tier, BodyTier.NARROW)
        self.assertTrue(n.needs_review)
        self.assertIn(RC.FLEET_NO_TYPE_INFO, codes(n))

    def test_exclusion_fallback(self):
        # a widebody-only pool fully excluded falls back to narrow pool minus exclusions (review)
        ar = self.cfg.airport_rules
        cands = [a for a, ex in ar.fleet_exclusions.items() if ex >= self.cfg.fleet_rules.cca_widebody["4D"]]
        if not cands:
            self.skipTest("no airport excludes all 4D widebodies")
        r = self.rec("CCA", "ZBAA", cands[0], fd2=["763"])
        self.assertTrue(r.needs_review)


class AuditTest(unittest.TestCase):
    def test_audit(self):
        cfg = S.cfg()
        rec = F.recommend_fleets(cfg, "CCA", "ZBAA", "ZSSS", route_type="scheduled", plan_fd2=["320"])
        cur = set(rec.fleet_ids) - {"26071"} | {"26077", "26728", "27929"}
        a = F.audit_fleets(cfg, cur, rec)
        self.assertEqual((a.extra, a.missing, a.dirty, a.air_force),
                         (frozenset({"26077"}), frozenset({"26071"}), frozenset({"26728"}), frozenset({"27929"})))
        self.assertFalse(a.ok)
        self.assertEqual([r.code for r in a.reasons], [RC.FLEET_DIRTY, RC.FLEET_AIR_FORCE, RC.FLEET_EXTRA,
                                                       RC.FLEET_MISSING])
        self.assertTrue(F.audit_fleets(cfg, rec.fleet_ids, rec).ok)


if __name__ == "__main__":
    unittest.main()
