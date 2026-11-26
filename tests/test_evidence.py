"""evidence: probe states (plan 5.3), bank clustering + supersede (5.4), weekday/week statistics, FD5-as-UTC."""

import dataclasses
import sys
import unittest

from tests import _support as S

sys.path.insert(0, str(S.WORK_DIR / "tests" / "fixtures" / "reconcile"))
import vcca_synth as X  # noqa: E402

from vcca import evidence as E  # noqa: E402
from vcca.fr24_obs import build_observations  # noqa: E402
from vcca.models import EvidenceState as ES, StoppedKind  # noqa: E402

MWF = {0, 2, 4}
# an unrelated actual series so that latest_ts (and the recent window) is ~2026-11-24
ANCHOR = X.series("CA9999", "CCA9999", "ZBAA", "ZGGG", "01:00", set(range(7)))


def ix(rows, fetched=()):
    return build_observations(S.cfg(), rows + ANCHOR, cao_numbers=(), fetched_numbers=fetched)


def ev(rows, key, fetched=()):
    return E.build_evidence(key, ix(rows, fetched), S.cfg())


class StateTest(unittest.TestCase):
    def test_active_banks_and_weekday_stats(self):
        e = ev(X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:00", MWF), ("CCA", "CCA1", "ZBAA", "ZSSS"))
        self.assertEqual(e.state, ES.ACTIVE)
        self.assertEqual(len(e.banks), 1)
        b = e.banks[0]
        self.assertEqual((b.mode_dep, b.mode_arr, b.act_days), ("08:00", "10:00", frozenset(MWF)))
        self.assertTrue(all(len(w) >= 4 for w in b.day_weeks.values()))
        self.assertEqual(set(e.weekday_stats), MWF)
        self.assertGreaterEqual(e.season_weeks_observed, 4)
        self.assertEqual(e.fleet_ids, frozenset({"26072"}))
        self.assertEqual(e.acts, b.acts)

    def test_sched_only_and_stale(self):
        cur = X.series("CA2", "CCA2", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 11, 20), end=(2026, 11, 30),
                       actual=False)
        self.assertEqual(ev(cur, ("CCA", "CCA2", "ZBAA", "ZSSS")).state, ES.SCHED_ONLY)
        old = X.series("CA2", "CCA2", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 10, 26), end=(2026, 10, 31),
                       actual=False, status="unknown")
        e = ev(old, ("CCA", "CCA2", "ZBAA", "ZSSS"))
        self.assertEqual(e.state, ES.STALE_SCHED)
        self.assertTrue(e.info["last_sched"])

    def test_carrier_changed_needs_recent_acts(self):
        key = ("CCA", "CCA5", "ZBAA", "ZSSS")
        three = X.series("CA5", "BJN5", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 11, 16), end=(2026, 11, 21))
        e = ev(three, key)
        self.assertEqual(e.state, ES.CARRIER_CHANGED)
        self.assertEqual((e.info["new_op"], e.info["new_callsign"]), ("BJN", "BJN5"))
        two = three[:2]
        e2 = ev(two, key, fetched={"CA5"})
        self.assertNotEqual(e2.state, ES.CARRIER_CHANGED)
        self.assertIn("carrier_change_min_acts", e2.info["threshold_blocked"])
        self.assertEqual(e2.other_ops[0][:2], ("BJN", "BJN5"))
        # old (non-recent) other-op actuals do not count either
        old = X.series("CA5", "BJN5", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 10, 26), end=(2026, 11, 2))
        self.assertNotEqual(ev(old, key, fetched={"CA5"}).state, ES.CARRIER_CHANGED)

    def test_route_changed(self):
        e = ev(X.series("CA6", "CCA6", "ZBAA", "ZGGG", "08:00", MWF), ("CCA", "CCA6", "ZBAA", "ZSSS"))
        self.assertEqual(e.state, ES.ROUTE_CHANGED)
        self.assertEqual(e.other_ods[0][:2], ("ZBAA", "ZGGG"))

    def test_stopped_pre_season_kinds(self):
        pre = dict(start=(2026, 9, 1), end=(2026, 10, 20))
        e = ev(X.series("CA7", "CCA7", "ZBAA", "ZSSS", "08:00", MWF, **pre), ("CCA", "CCA7", "ZBAA", "ZSSS"))
        self.assertEqual((e.state, e.stopped_kind), (ES.STOPPED_PRE_SEASON, StoppedKind.SAME_OP))
        self.assertTrue(e.pre_last_act)
        e = ev(X.series("CA8", "BJN8", "ZBAA", "ZSSS", "08:00", MWF, **pre), ("CCA", "CCA8", "ZBAA", "ZSSS"))
        self.assertEqual(e.stopped_kind, StoppedKind.OTHER_OP)
        e = ev(X.series("CA9", "CCA9", "ZBAA", "ZGGG", "08:00", MWF, **pre), ("CCA", "CCA9", "ZBAA", "ZSSS"))
        self.assertEqual(e.stopped_kind, StoppedKind.OTHER_ROUTE)

    def test_never_flown_and_no_data(self):
        self.assertEqual(ev([], ("CCA", "CCA11", "ZBAA", "ZSSS"), fetched={"CA11"}).state, ES.NEVER_FLOWN)
        self.assertEqual(ev([], ("CCA", "CCA12", "ZBAA", "ZSSS")).state, ES.NO_DATA)

    def test_real_sample_states(self):
        inp = X.fixture_inputs(S.cfg())
        self.assertEqual(inp.evidence[("CCA", "CCA754", "UUEE", "ZBAA")].state, ES.ACTIVE)
        self.assertGreaterEqual(inp.evidence[("CCA", "CCA754", "UUEE", "ZBAA")].acts, 5)
        e = E.build_evidence(("CAO", "CAO1006", "LEMD", "ZSPD"), inp.index, S.cfg())
        self.assertEqual(e.state, ES.ACTIVE)
        self.assertTrue(any(o.number == "" for o in e.observations))   # number-less CAO rows count


class BankTest(unittest.TestCase):
    th = S.cfg().thresholds

    def banks(self, rows):
        return ev(rows, ("CCA", "CCA1", "ZBAA", "ZSSS")).banks

    def test_merge_tolerance(self):
        a = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:00", {0, 2})
        b = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:30", {4})
        self.assertEqual(len(self.banks(a + b)), 1)                     # 30/30 <= 45/60
        c = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "09:00", {4})
        self.assertEqual(len(self.banks(a + c)), 2)                     # 60/60: no merge rule fits
        bk = self.banks(a + c)
        self.assertEqual(bk[0].mode_dep, "08:00")                       # sorted by acts desc

    def test_real_time_fallback_rounding(self):
        rows = [dict(r, sched_dep=None, sched_arr=None) for r in X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:02", MWF)]
        b = self.banks(rows)[0]
        self.assertEqual(b.mode_dep, "08:05")                           # real = sched+5min = 08:07 -> 08:05

    def test_mid_season_retime_supersede(self):
        old = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:00", MWF, end=(2026, 11, 3))
        new = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "11:00", MWF, start=(2026, 11, 5))
        bk = self.banks(old + new)
        self.assertEqual([b.mode_dep for b in bk], ["11:00"])
        self.assertEqual(bk[0].superseded[0]["mode_dep"], "08:00")

    def test_no_supersede_by_future_schedules_only(self):
        old = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:00", MWF, end=(2026, 11, 20))
        fut = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "13:00", MWF, start=(2026, 11, 21), end=(2026, 12, 20),
                       actual=False)
        self.assertEqual(sorted(b.mode_dep for b in self.banks(old + fut)), ["08:00", "13:00"])

    def test_calendar_entries(self):
        from vcca.models import EvidenceSummary
        flown = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:00", {0}, end=(2026, 11, 10))
        fut = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "08:00", {0}, start=(2026, 11, 30), end=(2026, 11, 30),
                       actual=False)
        # 23:55 sample of a 00:10 bank belongs to the next UTC day (bank frame)
        late = [X.row("CA1", "CCA1", "ZBAA", "ZSSS", X.ts(2026, 11, 3, 23, 55))]
        early = X.series("CA1", "CCA1", "ZBAA", "ZSSS", "00:10", {2}, end=(2026, 11, 20))
        cal = EvidenceSummary.from_evidence(ev(flown + fut + late + early, ("CCA", "CCA1", "ZBAA", "ZSSS"))).cal
        self.assertIn("2026-10-26 08:00 A", cal)
        self.assertIn("2026-11-30 08:00 S", cal)
        self.assertIn("2026-11-04 23:55 A", cal)
        self.assertEqual(cal, sorted(cal))
        self.assertFalse(any(e.startswith("2026-11-03 23:55") for e in cal))

    def test_cao3155_real_supersede(self):
        inp = X.fixture_inputs(S.cfg())
        e = inp.evidence[("CAO", "CAO3155", "ZUUU", "ESSA")]
        self.assertEqual(e.banks[0].label, "20:15-06:25")
        self.assertIn("18:00", [x["mode_dep"] for x in e.banks[0].superseded])
        self.assertNotIn("18:00", [b.mode_dep for b in e.banks])


class Fd5Test(unittest.TestCase):
    def test_fd5_written_as_utc(self):
        # FR24 flies 06:00-08:00Z. Plan wrote FD5 = 06:00 (UTC by mistake) and FD6 = 16:00 (BJT).
        e = ev(X.series("CA1", "CCA1", "ZBAA", "ZSSS", "06:00", MWF), ("CCA", "CCA1", "ZBAA", "ZSSS"))
        l = X.leg("CCA1", "ZBAA", "ZSSS", "22:00", "08:00", {6, 1, 3})
        l = dataclasses.replace(l, bjt_dep="06:00", bjt_arr="16:00", bjt_days="1.3.5..")
        c = E.fd5_utc_check(l, e)
        self.assertTrue(c.suspect)
        self.assertEqual((c.utc_dep, c.utc_arr, c.days), ("06:00", "08:00", frozenset({0, 2, 4})))
        ok = X.leg("CCA1", "ZBAA", "ZSSS", "06:00", "08:00", MWF)
        c2 = E.fd5_utc_check(ok, e)
        self.assertFalse(c2.suspect)
        self.assertEqual(c2.dep_diff, 0)
        self.assertFalse(E.fd5_utc_check(ok, None).suspect)

    def test_unplanned_keys(self):
        i = ix(X.series("CA1", "CCA1", "ZBAA", "ZSSS", "06:00", MWF))
        self.assertEqual(E.unplanned_keys(i, [("CCA", "CCA9999", "ZBAA", "ZGGG")]), [("CCA", "CCA1", "ZBAA", "ZSSS")])


if __name__ == "__main__":
    unittest.main()
