"""reconcile: calibration (5.5), single-day promotion (5.6), decision tables A-E (5.8), closure, idempotency.

Real samples (tests/fixtures/reconcile, cut from the legacy cache): CCA1109 micro-split, CCA754 single-day promotion,
CAO3155 mid-season retime + rotation-gap leg, CAO1006 number-less rows. Synthetic worlds cover every table row.
"""

import os
import sys
import unittest

from tests import _support as S

sys.path.insert(0, str(S.WORK_DIR / "tests" / "fixtures" / "reconcile"))
import vcca_synth as X  # noqa: E402

from vcca import reconcile as R  # noqa: E402
from vcca.fleets import recommend_fleets  # noqa: E402
from vcca.models import DecisionCategory as C, ReasonCode as RC, VROUTE_CATEGORIES  # noqa: E402

MWF = {0, 2, 4}
ALL = set(range(7))
ANCHOR = X.series("CA9999", "CCA9999", "ZBAA", "ZGGG", "01:00", ALL)   # keeps latest_ts at 2026-11-24
ANCHOR_V = X.vroute("v9999", "CCA9999", "ZBAA", "ZGGG", "01:00", "03:00", ALL)


def cfg():
    return S.cfg()


def rec_fleets(o="ZBAA", d="ZSSS", fd2=("320",)):
    return tuple(sorted(recommend_fleets(cfg(), "CCA", o, d, route_type="scheduled", plan_fd2=fd2,
                                         fr24_fleet_ids={"26072"}).fleet_ids))


class World:
    def __init__(self, legs=(), routes=(), rows=(), fetched=(), airports=None):
        snap = X.snapshot(list(routes) + [ANCHOR_V], **({"airports": airports} if airports else {}))
        self.inp = X.inputs(cfg(), list(legs), snap, list(rows) + ANCHOR, fetched=fetched)
        self.res = X.run(cfg(), self.inp)
        self.ds = self.res.decisions

    def vid(self, rid):
        return next(d for d in self.ds if d.vroute_id == str(rid))

    def key(self, cs, cat=None):
        return [d for d in self.ds if d.target.callsign == cs and (cat is None or d.category == cat)]

    def codes(self, d):
        return {r.code for r in d.reasons}

    def closure(self):
        return R.check_closure(self.ds, self.inp.vamsys)

    def idem(self):
        ch, _ = R.idempotency_check(cfg(), self.inp, self.res, now=X.NOW)
        return ch


# =====================================================================================================================
class FixtureWorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inp = X.fixture_inputs(cfg())
        cls.res = X.run(cfg(), cls.inp)
        cls.by_vid = {d.vroute_id: d for d in cls.res.decisions if d.vroute_id}

    def test_cca1109_micro_split(self):
        g = self.inp.groups[("CCA", "CCA1109", "ZBAA", "ZBXH")]
        slots = R.calibrate_slots(cfg(), g, self.inp.evidence[g.key])
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0].dep, slots[0].days), ("02:55", frozenset(ALL)))   # dominant sub-slot (Mon-Sat)
        d = self.by_vid["11507810"]
        self.assertEqual(d.category, C.KEEP)
        self.assertIn(RC.MICRO_SPLIT_MERGED, {r.code for r in d.reasons})

    def test_cca754_single_day_promotion(self):
        d = self.by_vid["11695316"]
        self.assertIn(d.category, (C.KEEP, C.UPDATE))
        self.assertEqual(d.target.days, frozenset(ALL))
        self.assertIn(RC.SINGLE_DAY_PROMOTED, {r.code for r in d.reasons})
        self.assertGreaterEqual(self.res.threshold_hits["single_day_promote_acts"], 1)

    def test_cao3155_retime_supersede(self):
        self.assertEqual(self.by_vid["14432894"].category, C.KEEP)
        self.assertIn(RC.BANK_SUPERSEDED, {r.code for r in self.by_vid["14432894"].reasons})
        old = self.by_vid["11508597"]
        self.assertEqual(old.category, C.RETIRE_REDUNDANT)
        self.assertEqual([c.field for c in old.changes], ["end_date"])

    def test_cao3155_rotation_gap_leg(self):
        ds = [d for d in self.res.decisions if d.target.key == ("CAO", "CAO3155", "ESSA", "EBLG")]
        self.assertTrue(ds and all(d.category == C.CREATE_FR24 for d in ds))
        self.assertTrue(all(RC.ROTATION_GAP_LEG in {r.code for r in d.reasons} for d in ds))
        days = [d.target.days for d in ds]
        self.assertEqual(sum(len(x) for x in days), len(frozenset().union(*days)))      # each weekday in one bank
        by_dep = {d.target.dep: d.target.days for d in ds}
        self.assertEqual(by_dep.get("07:25"), frozenset({2}))

    def test_cao1006_numberless_rows_used(self):
        ds = [d for d in self.res.decisions if d.target.key == ("CAO", "CAO1006", "LEMD", "ZSPD")]
        self.assertTrue(ds)
        self.assertTrue(all(d.category == C.CREATE_PLAN for d in ds))
        self.assertEqual(sorted(d.target.dep for d in ds), ["16:55", "17:40"])

    def test_zsja_alias_route_matched(self):
        self.assertEqual(self.by_vid["11508930"].target.dest, "ZSGS")
        self.assertEqual(self.by_vid["11508930"].category, C.KEEP)

    def test_closure_and_idempotency(self):
        self.assertEqual(R.check_closure(self.res.decisions, self.inp.vamsys), [])
        ch, _ = R.idempotency_check(cfg(), self.inp, self.res, now=X.NOW)
        self.assertEqual(ch, {})


# =====================================================================================================================
class TableATest(unittest.TestCase):
    """5.8 A: plan group x VAMSYS."""

    def test_create_plan_and_watch(self):
        w = World(legs=[X.leg("CCA101", "ZBAA", "ZSSS", "08:00", "10:00", MWF),
                        X.leg("CCA102", "ZBAA", "ZSSS", "09:00", "11:00", MWF)],
                  rows=X.series("CA101", "CCA101", "ZBAA", "ZSSS", "08:00", MWF), fetched={"CA102"})
        c = w.key("CCA101")[0]
        self.assertEqual(c.category, C.CREATE_PLAN)
        self.assertEqual((c.target.days, c.target.cost_index, c.vroute_id), (frozenset(MWF), 80, None))
        self.assertEqual(c.target.start_date, w.res.meta.effective_time)
        self.assertTrue(c.target.fleet_ids)
        wt = w.key("CCA102")[0]
        self.assertEqual(wt.category, C.WATCH_PLAN)
        self.assertIn(RC.PLAN_NOT_FLOWN, w.codes(wt))

    def test_update_keep_redundant_bankmatch_extra(self):
        fl = rec_fleets()
        legs = [X.leg("CCA103", "ZBAA", "ZSSS", "08:00", "10:00", MWF),
                X.leg("CCA103", "ZBAA", "ZSSS", "16:00", "18:00", {1, 3}),
                X.leg("CCA104", "ZBAA", "ZSSS", "06:00", "08:00", MWF)]
        rows = (X.series("CA103", "CCA103", "ZBAA", "ZSSS", "08:00", MWF)
                + X.series("CA103", "CCA103", "ZBAA", "ZSSS", "16:00", {1, 3})
                + [X.row("CA103", "CCA103", "ZBAA", "ZSSS", X.ts(2026, 11, 22, 12, 0))]        # lone 12:00 bank
                + X.series("CA104", "CCA104", "ZBAA", "ZSSS", "06:00", MWF))
        routes = [X.vroute(1, "CCA103", "ZBAA", "ZSSS", "08:10", "10:10", MWF, fleets=fl),     # KEEP (<=15 min)
                  X.vroute(2, "CCA103", "ZBAA", "ZSSS", "12:05", "14:05", {6}, fleets=fl),     # matches lone bank
                  X.vroute(3, "CCA103", "ZBAA", "ZSSS", "20:00", "22:00", {5}, fleets=fl),     # redundant
                  X.vroute(5, "CCA103", "ZBAA", "ZSSS", "16:00", "18:00", {1, 3}, fleets=fl),  # 2nd plan slot
                  X.vroute(4, "CCA104", "ZBAA", "ZSSS", "06:00", "08:00", MWF, fleets=("26077",), cost_index="66")]
        w = World(legs=legs, routes=routes, rows=rows)
        self.assertEqual(w.vid(1).category, C.KEEP)
        self.assertEqual((w.vid(1).target.dep, w.vid(1).target.arr), ("08:10", "10:10"))
        self.assertIn(w.vid(2).category, (C.UPDATE, C.KEEP))
        self.assertIn(RC.VAMSYS_MATCHES_BANK, w.codes(w.vid(2)))
        self.assertEqual(w.vid(3).category, C.RETIRE_REDUNDANT)
        self.assertEqual(w.vid(3).target.end_date, w.res.meta.effective_time)
        u = w.vid(4)
        self.assertEqual(u.category, C.UPDATE)
        self.assertEqual([c.field for c in u.changes], ["fleet_ids"])
        self.assertEqual(u.target.cost_index, 66)                       # Cost Index kept on UPDATE
        self.assertIn(RC.FLEET_EXTRA, w.codes(u))
        self.assertEqual(w.vid(5).category, C.KEEP)
        self.assertEqual(w.key("CCA103", C.CREATE_EXTRA_SLOT), [])
        self.assertEqual(w.closure(), [])
        self.assertEqual(w.idem(), {})
        # one VAMSYS route for two flown plan slots -> CREATE_EXTRA_SLOT for the unpaired one
        w2 = World(legs=legs[:2], routes=routes[:1], rows=rows[:-len(X.series("CA104", "CCA104", "ZBAA", "ZSSS",
                                                                               "06:00", MWF))])
        extra = w2.key("CCA103", C.CREATE_EXTRA_SLOT)
        self.assertEqual([(d.target.dep, d.target.days) for d in extra], [("16:00", frozenset({1, 3}))])
        self.assertEqual(w2.idem(), {})

    def test_retire_deferred(self):
        w = World(legs=[X.leg("CCA105", "ZBAA", "ZSSS", "08:00", "10:00", MWF)],
                  routes=[X.vroute(5, "CCA105", "ZBAA", "ZSSS", "08:00", "10:00", MWF)],
                  rows=X.series("CA105", "CCA105", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 9, 1), end=(2026, 10, 20)))
        d = w.vid(5)
        self.assertEqual(d.category, C.RETIRE_DEFERRED)
        self.assertIn(RC.DEFERRED_PLAN_NOT_FLOWN, w.codes(d))

    def test_carrier_change_matched_via_old_operator(self):
        w = World(legs=[X.leg("CCA106", "ZBAA", "ZSSS", "08:00", "10:00", MWF)],
                  routes=[X.vroute(6, "CCA106", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fleets=("26718",))],
                  rows=X.series("CA106", "BJN106", "ZBAA", "ZSSS", "08:00", MWF, ac="B738"))
        d = w.vid(6)
        self.assertEqual(d.category, C.UPDATE)
        self.assertEqual((d.target.callsign, d.target.number), ("BJN106", "CA106"))
        self.assertEqual({c.field for c in d.changes}, {"callsign"})
        self.assertTrue({RC.CARRIER_CHANGED, RC.MATCHED_VIA_OLD_OPERATOR} <= w.codes(d))
        self.assertEqual(w.idem(), {})

    def test_micro_split_time_override_weekday_rules(self):
        legs = [X.leg("CCA107", "ZBAA", "ZSSS", "02:40", "04:05", {6}),
                X.leg("CCA107", "ZBAA", "ZSSS", "02:55", "04:20", {0, 1, 2, 3, 4, 5}),
                X.leg("CCA108", "ZBAA", "ZSSS", "08:00", "10:00", {0, 2, 4, 5}),          # Sat never flown
                X.leg("CCA109", "ZBAA", "ZSSS", "12:00", "14:00", MWF)]                    # FR24 13:00
        rows = (X.series("CA107", "CCA107", "ZBAA", "ZSSS", "02:55", ALL, block_min=85)
                + X.series("CA108", "CCA108", "ZBAA", "ZSSS", "08:00", {0, 1, 2, 4})       # Tue flown 4+ weeks
                + [X.row("CA108", "CCA108", "ZBAA", "ZSSS", X.ts(2026, 11, 19, 8))]       # one Thu: not >= 2 weeks
                + X.series("CA109", "CCA109", "ZBAA", "ZSSS", "13:00", MWF))
        w = World(legs=legs, rows=rows)
        d7 = w.key("CCA107")
        self.assertEqual(len(d7), 1)
        self.assertEqual((d7[0].target.dep, d7[0].target.days), ("02:55", frozenset(ALL)))
        self.assertIn(RC.MICRO_SPLIT_MERGED, w.codes(d7[0]))
        d8 = w.key("CCA108")[0]
        self.assertEqual(d8.target.days, frozenset({0, 1, 2, 4}))
        self.assertTrue({RC.WEEKDAY_ADDED, RC.WEEKDAY_DROPPED} <= w.codes(d8))
        self.assertTrue(d8.needs_review)
        self.assertIn(str(RC.WEEKDAY_DROPPED), d8.review_flags)
        d9 = w.key("CCA109")[0]
        self.assertEqual(d9.target.dep, "13:00")
        self.assertIn(RC.TIME_CALIBRATED, w.codes(d9))
        hits = w.res.threshold_hits
        for k in ("time_override_min_acts", "weekday_add_min_weeks", "weekday_drop_min_weeks", "plan_attach"):
            self.assertGreaterEqual(hits[k], 1, k)

    def test_single_day_rows(self):
        legs = [X.leg("CCA110", "ZBAA", "ZSSS", "08:00", "10:00", {6}, single=True),
                X.leg("CCA111", "ZBAA", "ZSSS", "09:00", "11:00", {6}, single=True),
                X.leg("CCA112", "ZBAA", "ZSSS", "10:00", "12:00", {6}, single=True),
                X.leg("CCA112", "ZBAA", "ZSSS", "10:00", "12:00", MWF)]
        rows = (X.series("CA110", "CCA110", "ZBAA", "ZSSS", "08:00", {1, 3})              # >= 5 acts
                + X.series("CA111", "CCA111", "ZBAA", "ZSSS", "09:00", {6}, end=(2026, 11, 8))   # 2 acts
                + X.series("CA112", "CCA112", "ZBAA", "ZSSS", "10:00", MWF))
        w = World(legs=legs, rows=rows)
        p = w.key("CCA110")[0]
        self.assertEqual((p.category, p.target.days), (C.CREATE_PLAN, frozenset({1, 3})))
        self.assertIn(RC.SINGLE_DAY_PROMOTED, w.codes(p))
        ig = w.key("CCA111")[0]
        self.assertEqual(ig.category, C.WATCH_PLAN)
        self.assertIn(RC.SINGLE_DAY_IGNORED, w.codes(ig))
        reg = w.key("CCA112")
        self.assertEqual([(d.category, d.target.days) for d in reg], [(C.CREATE_PLAN, frozenset(MWF))])

    def test_unflown_plan_slot_not_created(self):
        legs = [X.leg("CCA113", "ZBAA", "ZSSS", "08:00", "10:00", MWF),
                X.leg("CCA113", "ZBAA", "ZSSS", "18:00", "20:00", {1, 3})]     # FR24 never shows 18:00
        w = World(legs=legs, rows=X.series("CA113", "CCA113", "ZBAA", "ZSSS", "08:00", MWF))
        cats = sorted((d.category, d.target.dep) for d in w.key("CCA113"))
        self.assertEqual(cats, [(C.CREATE_PLAN, "08:00"), (C.WATCH_PLAN, "18:00")])
        self.assertIn(RC.PLAN_SLOT_NOT_FLOWN, w.codes([d for d in w.key("CCA113") if d.target.dep == "18:00"][0]))

    def test_extra_slot_weekday_dedup_vs_double_operation(self):
        legs = [X.leg("CCA114", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        drift = (X.series("CA114", "CCA114", "ZBAA", "ZSSS", "08:00", {0, 2})
                 + X.series("CA114", "CCA114", "ZBAA", "ZSSS", "08:00", {4}, end=(2026, 11, 8))
                 + X.series("CA114", "CCA114", "ZBAA", "ZSSS", "15:00", {4}, start=(2026, 11, 9)))   # Fri drifts late
        w = World(legs=legs, rows=drift)
        self.assertEqual([(d.category, d.target.days) for d in w.key("CCA114")], [(C.CREATE_PLAN, frozenset(MWF))])
        self.assertGreaterEqual(w.res.threshold_hits["extra_slot_weekday_dedup"], 1)
        # a weekday that consistently flies at another time moves to its own slot (dropped here, review)
        moved = X.series("CA116", "CCA116", "ZBAA", "ZSSS", "08:00", {0, 2}) + X.series(
            "CA116", "CCA116", "ZBAA", "ZSSS", "15:00", {4})
        w3 = World(legs=[X.leg("CCA116", "ZBAA", "ZSSS", "08:00", "10:00", MWF)], rows=moved)
        self.assertEqual(sorted((d.target.dep, d.target.days) for d in w3.key("CCA116")),
                         [("08:00", frozenset({0, 2})), ("15:00", frozenset({4}))])
        legs2 = [X.leg("CCA115", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        double = X.series("CA115", "CCA115", "ZBAA", "ZSSS", "08:00", MWF) + X.series(
            "CA115", "CCA115", "ZBAA", "ZSSS", "15:00", {4})           # Fri twice a day
        w2 = World(legs=legs2, rows=double)
        self.assertEqual(sorted((d.category, d.target.dep, d.target.days) for d in w2.key("CCA115")),
                         [(C.CREATE_PLAN, "08:00", frozenset(MWF)), (C.CREATE_PLAN, "15:00", frozenset({4}))])
        self.assertTrue([d for d in w2.key("CCA115") if d.details.get("fr24_extra_slot")])


# =====================================================================================================================
class TableBTest(unittest.TestCase):
    """5.8 B: VAMSYS route not in the plan."""

    def test_keep_unplanned_and_update(self):
        fl = rec_fleets(fd2=())
        rows = (X.series("CA201", "CCA201", "ZBAA", "ZSSS", "08:00", MWF)
                + X.series("CA202", "CCA202", "ZBAA", "ZSSS", "09:00", MWF))
        w = World(routes=[X.vroute(201, "CCA201", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fleets=fl),
                          X.vroute(202, "CCA202", "ZBAA", "ZSSS", "09:30", "11:30", {0, 2}, fleets=fl)], rows=rows)
        self.assertEqual(w.vid(201).category, C.KEEP_UNPLANNED)
        u = w.vid(202)
        self.assertEqual(u.category, C.UPDATE)
        self.assertEqual((u.target.dep, u.target.days), ("09:00", frozenset(MWF)))
        self.assertIn(RC.UNPLANNED_ACTIVE, w.codes(u))
        self.assertEqual(w.idem(), {})

    def test_hold_rotation_gap_and_no_data(self):
        legs = [X.leg("CAO303", "ZSPD", "ESSA", "18:00", "06:00", {1}, op="CAO"),
                X.leg("CAO304", "EBLG", "ZSPD", "12:00", "22:00", {2}, op="CAO")]
        w = World(legs=legs, routes=[X.vroute(303, "CAO303", "ESSA", "EBLG", "08:00", "10:00", {2}, fleets=("26722",)),
                                     X.vroute(205, "CCA205", "ZBAA", "ZSSS", "08:00", "10:00", MWF)],
                  fetched={"CA303", "CA304"})
        h = w.vid(303)
        self.assertEqual(h.category, C.HOLD)
        self.assertIn(RC.HOLD_ROTATION_GAP_BLIND, w.codes(h))
        n = w.vid(205)
        self.assertEqual(n.category, C.HOLD)
        self.assertIn(RC.NO_DATA_REFETCH, w.codes(n))
        self.assertTrue(n.needs_review)

    def test_retire_reasons(self):
        pre = dict(start=(2026, 9, 1), end=(2026, 10, 20))
        rows = (X.series("CA211", "BJN211", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 11, 10))   # carrier change
                + X.series("CA212", "CCA212", "ZBAA", "ZGGG", "08:00", MWF)                        # other leg
                + X.series("CA213", "CCA213", "ZBAA", "ZSSS", "08:00", MWF, **pre)                 # stopped
                + X.series("CA215", "CCA215", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 10, 26),
                           end=(2026, 10, 31), actual=False, status="unknown"))                    # stale sched
        routes = [X.vroute(rid, f"CCA{rid}", "ZBAA", "ZSSS", "08:00", "10:00", MWF) for rid in range(211, 216)]
        w = World(routes=routes, rows=rows, fetched={"CA214"})
        exp = {211: RC.RETIRE_CARRIER_CHANGED, 212: RC.RETIRE_ROUTE_CHANGED, 213: RC.RETIRE_STOPPED_PRE_SEASON,
               214: RC.RETIRE_NEVER_FLOWN, 215: RC.RETIRE_STALE_SCHED}
        for rid, code in exp.items():
            d = w.vid(rid)
            self.assertEqual(d.category, C.RETIRE, rid)
            self.assertIn(code, w.codes(d), rid)
            self.assertEqual(d.changes[0].field, "end_date")
        self.assertEqual(w.closure(), [])
        self.assertEqual(w.idem(), {})

    def test_occasional_old_carrier(self):
        legs = [X.leg("BJN220", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        rows = (X.series("CA220", "BJN220", "ZBAA", "ZSSS", "08:00", MWF, ac="B738")
                + [X.row("CA220", "CCA220", "ZBAA", "ZSSS", X.ts(2026, 11, 3, 8))])
        w = World(legs=legs, routes=[X.vroute(220, "CCA220", "ZBAA", "ZSSS", "08:00", "10:00", MWF)], rows=rows)
        d = w.vid(220)
        self.assertEqual(d.category, C.RETIRE)
        self.assertIn(RC.RETIRE_OCCASIONAL_OLD_CARRIER, w.codes(d))


# =====================================================================================================================
class TableCDTest(unittest.TestCase):
    """5.8 C (FR24 only) and D (airports)."""

    def test_create_fr24_charter_positioning(self):
        rows = (X.series("CA301", "CCA301", "ZBAA", "ZSSS", "08:00", MWF)
                + [X.row("CA302", "CCA302", "ZBAA", "ZSSS", X.ts(2026, 11, 5, 8)),
                   X.row("CA302", "CCA302", "ZBAA", "ZSSS", X.ts(2026, 11, 12, 8))]          # 2 acts
                + X.series("CA303", "CCA303", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 11, 16), end=(2026, 11, 21))
                + [X.row(None, "CCA123R", "ZBAA", "ZBAD", X.ts(2026, 11, 5, 8), source="reg:B-1")])
        w = World(rows=rows)
        c = w.key("CCA301")[0]
        self.assertEqual((c.category, c.target.days, c.target.dep), (C.CREATE_FR24, frozenset(MWF), "08:00"))
        self.assertIn(RC.FR24_ONLY_ROUTE, w.codes(c))
        self.assertEqual(w.key("CCA302")[0].category, C.CHARTER)
        self.assertEqual(len(w.key("CCA302")[0].evidence.dates), 2)
        self.assertEqual(w.key("CCA303")[0].category, C.CHARTER)        # 3 acts but one week only
        self.assertEqual(w.key("CCA123R")[0].category, C.POSITIONING)
        self.assertGreaterEqual(w.res.threshold_hits["fr24_add_min_acts/min_weeks"], 2)
        self.assertEqual(w.idem(), {})

    def test_rotation_gap_one_flight_creates(self):
        legs = [X.leg("CAO305", "ZSPD", "ESSA", "18:00", "06:00", {1}, op="CAO"),
                X.leg("CAO306", "EBLG", "ZSPD", "12:00", "22:00", {2}, op="CAO")]
        r = X.row(None, "CAO305", "ESSA", "EBLG", X.ts(2026, 11, 18, 7, 30), ac="B77L", airline="CAO",
                  source="reg:B-2091")
        w = World(legs=legs, rows=[r], fetched={"CA305", "CA306"})
        d = [x for x in w.key("CAO305") if x.target.orig == "ESSA"][0]
        self.assertEqual(d.category, C.CREATE_FR24)
        self.assertIn(RC.ROTATION_GAP_LEG, w.codes(d))
        self.assertEqual(d.target.fleet_ids, tuple(sorted(cfg().fleet_rules.fixed_fleets["CAO"])))
        self.assertEqual(d.target.cost_index, 150)

    def test_airport_add(self):
        w = World(legs=[X.leg("CCA401", "ZBAA", "ZBAL", "08:00", "10:00", MWF)],
                  rows=X.series("CA401", "CCA401", "ZBAA", "ZBAL", "08:00", MWF))
        a = [d for d in w.ds if d.category == C.AIRPORT_ADD]
        self.assertEqual([(d.target.orig, d.details["name"]) for d in a], [("ZBAL", "BAYANHOT")])
        c = w.key("CCA401")[0]
        self.assertEqual(c.depends_on_airports, ["ZBAL"])
        self.assertIn(RC.DEPENDS_ON_AIRPORT_ADD, w.codes(c))
        self.assertEqual(w.idem(), {})


# =====================================================================================================================
class TableETest(unittest.TestCase):
    """5.8 E: idempotency rules, closure, apply."""

    def test_end_date_rules(self):
        fl = rec_fleets()
        legs = [X.leg("CCA501", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        rows = X.series("CA501", "CCA501", "ZBAA", "ZSSS", "08:00", MWF)
        routes = [X.vroute(501, "CCA501", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fleets=fl, end_date="2026-11-20 16:00:00"),
                  X.vroute(502, "CCA502", "ZBAA", "ZSSS", "08:00", "10:00", MWF, end_date="2026-11-20 16:00:00")]
        w = World(legs=legs, routes=routes, rows=rows, fetched={"CA502"})
        u = w.vid(501)
        self.assertEqual((u.category, [c.field for c in u.changes], u.target.end_date), (C.UPDATE, ["end_date"], ""))
        self.assertIn(str(RC.END_DATE_CLEARED), u.review_flags)
        r = w.vid(502)
        self.assertEqual((r.category, r.changes), (C.RETIRE, []))
        self.assertIn(RC.ALREADY_RETIRING, w.codes(r))
        self.assertEqual(R.effective_changes([r]), {})
        self.assertEqual(w.idem(), {})

    def test_end_dated_route_not_preferred_in_pairing(self):
        fl = rec_fleets()
        legs = [X.leg("CCA503", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        rows = X.series("CA503", "CCA503", "ZBAA", "ZSSS", "08:00", MWF)
        routes = [X.vroute(503, "CCA503", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fleets=fl, end_date="2026-11-20 16:00:00"),
                  X.vroute(504, "CCA503", "ZBAA", "ZSSS", "08:30", "10:30", MWF, fleets=fl)]
        w = World(legs=legs, routes=routes, rows=rows)
        self.assertEqual(w.vid(504).category, C.UPDATE)                 # live route takes the slot
        self.assertEqual(w.vid(503).category, C.RETIRE_REDUNDANT)
        self.assertEqual(w.vid(503).changes, [])

    def test_closure_errors_detected(self):
        w = World(routes=[X.vroute(601, "CCA601", "ZBAA", "ZSSS", "08:00", "10:00", MWF)], fetched={"CA601"})
        self.assertEqual(w.closure(), [])
        ds = [d for d in w.ds if d.vroute_id != "601"]
        self.assertTrue(any("601" in e for e in R.check_closure(ds, w.inp.vamsys)))
        dup = w.ds + [w.vid(601)]
        self.assertTrue(any("2 条决策" in e for e in R.check_closure(dup, w.inp.vamsys)))
        self.assertTrue(all(d.vroute_id for d in w.ds if d.category in VROUTE_CATEGORIES))

    def test_apply_and_target_states(self):
        legs = [X.leg("CCA701", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        w = World(legs=legs, rows=X.series("CA701", "CCA701", "ZBAA", "ZSSS", "08:00", MWF),
                  routes=[X.vroute(702, "CCA702", "ZBAA", "ZSSS", "08:00", "10:00", MWF)], fetched={"CA702"})
        routes = R.apply_decisions(w.inp.vamsys.routes, w.ds)
        new = [r for r in routes if r.id.startswith("new:")]
        self.assertEqual([(r.callsign, r.dep, r.days) for r in new], [("CCA701", "08:00", frozenset(MWF))])
        self.assertTrue([r for r in routes if r.id == "702"][0].end_date)
        ts = {t.query: t for t in w.res.target_states}
        self.assertEqual(str(ts["CA702"].priority), "P1")
        self.assertEqual(str(ts["CA9999"].priority), "P2")


# =====================================================================================================================
class ReviewFixesTest(unittest.TestCase):
    """Review of run 2026-11-26_1327: minority carrier, stale evidence, weekday-drop coverage, sched-only wording,
    meaningful review flags."""

    def test_minority_carrier_not_created(self):
        # planned CCD901 flies daily (recent); CCA901 flew 3x over 2 weeks a while ago -> report only
        legs = [X.leg("CCD901", "ZBAA", "ZSSS", "08:00", "10:00", ALL, fd2="738")]
        rows = (X.series("CA901", "CCD901", "ZBAA", "ZSSS", "08:00", ALL, ac="B738")
                + X.series("CA901", "CCA901", "ZBAA", "ZSSS", "08:00", {0}, start=(2026, 10, 26), end=(2026, 11, 9)))
        w = World(legs=legs, rows=rows)
        m = w.key("CCA901")
        self.assertEqual([d.category for d in m], [C.CHARTER])
        self.assertIn(RC.MIXED_OP_MINORITY, w.codes(m[0]))
        self.assertFalse(m[0].needs_review)
        self.assertIn(RC.MIXED_CARRIER, w.codes(w.key("CCD901")[0]))
        self.assertGreaterEqual(w.res.threshold_hits["mixed_op_minority"], 1)

    def test_majority_other_carrier_still_created(self):
        # plan lists BJN902 but only CCA902 flies recently (more recent actuals) -> CCA902 may be created
        legs = [X.leg("BJN902", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fd2="738")]
        rows = (X.series("CA902", "BJN902", "ZBAA", "ZSSS", "08:00", MWF, end=(2026, 10, 31), ac="B738")
                + X.series("CA902", "CCA902", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 11, 2)))
        w = World(legs=legs, rows=rows)
        self.assertEqual([d.category for d in w.key("CCA902")], [C.CREATE_FR24])

    def test_stale_evidence_does_not_create(self):
        legs = [X.leg("CCA903", "ZBAA", "ZSSS", "08:00", "10:00", ALL)]
        rows = (X.series("CA903", "CCA903", "ZBAA", "ZSSS", "08:00", ALL, end=(2026, 10, 28))
                + X.series("CA904", "CCA904", "ZBAA", "ZSSS", "09:00", {0, 3}, end=(2026, 11, 3)))   # 4 acts, 2 weeks
        w = World(legs=legs, rows=rows)
        p = w.key("CCA903")[0]
        self.assertEqual(p.category, C.WATCH_PLAN)
        self.assertIn(RC.NOT_RECENT, w.codes(p))
        self.assertEqual(p.reasons[[r.code for r in p.reasons].index(RC.NOT_RECENT)].params["last_date"][:10],
                         "2026-10-28")
        f = w.key("CCA904")[0]
        self.assertEqual(f.category, C.CHARTER)
        self.assertIn(RC.NOT_RECENT, w.codes(f))
        self.assertGreaterEqual(w.res.threshold_hits["recent_window_days(create)"], 2)
        self.assertEqual(w.idem(), {})

    def test_weekday_add_needs_recent_actual(self):
        legs = [X.leg("CCA905", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        rows = (X.series("CA905", "CCA905", "ZBAA", "ZSSS", "08:00", MWF, end=(2026, 11, 27))  # recent from 11-06
                + X.series("CA905", "CCA905", "ZBAA", "ZSSS", "08:00", {1}, end=(2026, 11, 3)))  # Tue: 2 old weeks
        w = World(legs=legs, rows=rows)
        d = w.key("CCA905")[0]
        self.assertEqual(d.target.days, frozenset(MWF))
        self.assertNotIn(RC.WEEKDAY_ADDED, w.codes(d))

    def test_weekday_drop_needs_coverage(self):
        # daily in the plan, FR24 sees each weekday only every other week and never Sunday -> keep Sunday, no review
        rows = []
        for wk, days in enumerate([{0, 2, 4}, {1, 3, 5}] * 3):
            rows += X.series("CA906", "CCA906", "ZBAA", "ZSSS", "08:00", days,
                             start=(2026, 10, 26 + 7 * wk) if 26 + 7 * wk <= 31 else (2026, 11, 26 + 7 * wk - 31),
                             end=(2026, 11, 1 + 7 * wk) if 1 + 7 * wk <= 30 else (2026, 12, 1 + 7 * wk - 30))
        w = World(legs=[X.leg("CCA906", "ZBAA", "ZSSS", "08:00", "10:00", ALL)], rows=rows)
        d = w.key("CCA906")[0]
        self.assertEqual(d.target.days, frozenset(ALL))
        self.assertFalse(d.needs_review)
        self.assertGreaterEqual(w.res.threshold_hits["weekday_drop_min_coverage(kept)"], 1)
        self.assertEqual(w.res.meta.thresholds["weekday_drop_min_coverage"], 0.75)

    def test_weekday_occurrences_ignore_partial_weeks(self):
        t0, t1 = X.ts(2026, 10, 25, 8), X.ts(2026, 11, 15, 8)      # Sun .. Sun
        self.assertEqual(R._weekday_occurrences(6, t0, t1), 4)
        self.assertEqual(R._weekday_occurrences(4, t0, t1), 3)
        self.assertEqual(R._weekday_occurrences(0, t1, t1), 0)

    def test_sched_only_wording(self):
        fl = rec_fleets()
        legs = [X.leg("CCA907", "ZBAA", "ZSSS", "08:00", "10:00", MWF)]
        rows = X.series("CA907", "CCA907", "ZBAA", "ZSSS", "08:00", MWF, start=(2026, 11, 20), end=(2026, 11, 30),
                        actual=False)
        w = World(legs=legs, routes=[X.vroute(907, "CCA907", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fleets=fl)],
                  rows=rows)
        codes = w.codes(w.vid(907))
        self.assertIn(RC.PLAN_FR24_SCHEDULED, codes)
        self.assertNotIn(RC.PLAN_FR24_CONFIRMED, codes)

    def test_report_only_rows_have_no_review(self):
        w = World(legs=[X.leg("CCA908", "ZBAA", "XXXX", "08:00", "10:00", MWF)], fetched={"CA908"})
        d = w.key("CCA908")[0]
        self.assertEqual(d.category, C.WATCH_PLAN)
        self.assertIn(RC.UNRATED_AIRPORT, w.codes(d))       # still explained ...
        self.assertFalse(d.needs_review)                    # ... but not sent to the review CSVs


class FreshDataFixesTest(unittest.TestCase):
    """Spot-check of the fresh-data run 2026-11-26_1404."""

    def test_zero_padded_callsign_is_regular(self):
        self.assertTrue(R._callsign_ok("CCA051", has_number=True))
        self.assertFalse(R._callsign_ok("CCA051", has_number=False))
        self.assertFalse(R._callsign_ok("CCA149X", has_number=True))
        self.assertFalse(R._callsign_ok("CCA0", has_number=True))
        w = World(rows=X.series("CA51", "CCA051", "ZBAA", "ZSSS", "08:00", MWF))
        self.assertEqual([d.category for d in w.key("CCA051")], [C.CREATE_FR24])

    def test_midnight_retime_keeps_bjt_weekdays(self):
        # plan 23:55Z Tue/Thu (= BJT Wed/Fri 07:55); FR24 moved to 00:25Z, i.e. UTC Wed/Fri (same BJT day)
        legs = [X.leg("CCA910", "ZBAA", "ZSSS", "23:55", "02:55", {1, 3})]
        rows = (X.series("CA910", "CCA910", "ZBAA", "ZSSS", "23:55", {1, 3}, end=(2026, 11, 3), block_min=180)
                + X.series("CA910", "CCA910", "ZBAA", "ZSSS", "00:25", {2, 4}, start=(2026, 11, 4), block_min=180))
        w = World(legs=legs, rows=rows)
        d = w.key("CCA910")[0]
        self.assertEqual((d.target.dep, d.target.days), ("00:25", frozenset({2, 4})))
        self.assertNotIn(RC.WEEKDAY_ADDED, w.codes(d))

    def test_kept_vamsys_time_across_midnight_shifts_days(self):
        fl = rec_fleets()
        legs = [X.leg("CCA911", "ZBAA", "ZSSS", "00:05", "02:05", {2, 4})]
        rows = X.series("CA911", "CCA911", "ZBAA", "ZSSS", "00:05", {2, 4})
        w = World(legs=legs, rows=rows,
                  routes=[X.vroute(911, "CCA911", "ZBAA", "ZSSS", "23:55", "01:55", {1, 3}, fleets=fl)])
        self.assertEqual(w.vid(911).category, C.KEEP)          # same flight, VAMSYS 10 min earlier on the eve
        self.assertEqual(w.idem(), {})

    def test_weekday_drop_uses_actuals_not_schedules(self):
        # plan Tue/Thu/Sat, FR24 FLIES Mon/Wed/Fri every week; placeholder schedules remain on Tue/Thu/Sat
        legs = [X.leg("CCA912", "ZBAA", "ZSSS", "09:00", "11:00", {1, 3, 5})]
        rows = (X.series("CA912", "CCA912", "ZBAA", "ZSSS", "09:00", MWF)
                + X.series("CA912", "CCA912", "ZBAA", "ZSSS", "09:00", {1, 3, 5}, actual=False))
        w = World(legs=legs, rows=rows)
        d = w.key("CCA912")[0]
        self.assertEqual(d.target.days, frozenset(MWF))
        self.assertTrue({RC.WEEKDAY_ADDED, RC.WEEKDAY_DROPPED} <= w.codes(d))

    def test_one_off_aircraft_does_not_flip_body_tier(self):
        legs = [X.leg("CCA913", "ZBAA", "ZSSS", "08:00", "10:00", ALL, fd2="")]
        rows = X.series("CA913", "CCA913", "ZBAA", "ZSSS", "08:00", ALL, ac="A333")
        rows[0]["aircraft_code"] = "A321"
        w = World(legs=legs, rows=rows)
        e = w.inp.evidence[("CCA", "CCA913", "ZBAA", "ZSSS")]
        self.assertEqual(e.fleet_ids, frozenset({"26078"}))
        self.assertIn("A321", e.info["ac_ignored"])
        self.assertFalse(set(w.key("CCA913")[0].target.fleet_ids) & {"26074", "26072"})
        # share is per body class: 2+2 widebodies of 20 flights (20%) keep the route mixed
        rows2 = X.series("CA915", "CCA915", "ZBAA", "ZSSS", "08:00", ALL, ac="A320")[:20]
        for i, c in enumerate(("A333", "A333", "A359", "A359")):
            rows2[i]["aircraft_code"] = c
        w2 = World(legs=[X.leg("CCA915", "ZBAA", "ZSSS", "08:00", "10:00", ALL, fd2="")], rows=rows2)
        self.assertTrue({"26078", "26080", "26072"} <= w2.inp.evidence[("CCA", "CCA915", "ZBAA", "ZSSS")].fleet_ids)

    def test_update_keep_carry_plan_slot(self):
        fl = rec_fleets()
        w = World(legs=[X.leg("CCA914", "ZBAA", "ZSSS", "08:00", "10:00", MWF)],
                  rows=X.series("CA914", "CCA914", "ZBAA", "ZSSS", "08:00", MWF),
                  routes=[X.vroute(914, "CCA914", "ZBAA", "ZSSS", "08:00", "10:00", MWF, fleets=fl)])
        self.assertEqual({k: w.vid(914).details[k] for k in ("plan_dep", "plan_arr", "plan_days")},
                         {"plan_dep": "08:00", "plan_arr": "10:00", "plan_days": sorted(MWF)})


# =====================================================================================================================
@unittest.skipUnless(os.environ.get("VCCA_TEST_FULL", "1") != "0" and (S.cfg().cache_dir / "flight_histories.json").is_file(),
                     "full-data acceptance (set VCCA_TEST_FULL=0 to skip)")
class FullDataAcceptanceTest(unittest.TestCase):
    """Plan 8 acceptance 1-2 on the real season data (legacy JSON or fr24_cache.db): closure + idempotency."""

    def test_closure_and_idempotency(self):
        c = cfg()
        inp = R.load_inputs(c, log=lambda s: None)
        res = R.run_reconcile(c, inp, now=X.NOW)
        self.assertEqual(R.check_closure(res.decisions, inp.vamsys), [])
        ch, res2 = R.idempotency_check(c, inp, res, now=X.NOW)
        self.assertEqual(dict(ch), {})
        self.assertEqual(sum(1 for d in res.decisions if d.category in VROUTE_CATEGORIES),
                         len(inp.vamsys.commercial_routes))


if __name__ == "__main__":
    unittest.main()
