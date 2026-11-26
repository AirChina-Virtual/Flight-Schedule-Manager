"""fr24_obs: attribution (callsign-first, CAO placeholders, foreign callsigns), filters, dedup, airport aliasing,
season / recent windows. Real samples come from tests/fixtures/reconcile (cut from the legacy cache)."""

import sys
import unittest

from tests import _support as S

sys.path.insert(0, str(S.WORK_DIR / "tests" / "fixtures" / "reconcile"))
import vcca_synth as X  # noqa: E402

from vcca import fr24_obs as F  # noqa: E402


def idx(rows, cao=(), **kw):
    return F.build_observations(S.cfg(), rows, cao_numbers=cao, **kw)


class NormalizeAirportTest(unittest.TestCase):
    def test_icao_iata_alias(self):
        alias = {"ZSJA": "ZSGS"}
        self.assertEqual(F.normalize_airport("zbaa", "PEK", alias=alias, iata_to_icao={}), "ZBAA")
        self.assertEqual(F.normalize_airport(None, "PEK", alias=alias, iata_to_icao={"PEK": "ZBAA"}), "ZBAA")
        self.assertEqual(F.normalize_airport(None, "XXX", alias=alias, iata_to_icao={}), "XXX")
        self.assertEqual(F.normalize_airport("ZSJA", None, alias=alias, iata_to_icao={}), "ZSGS")
        self.assertIsNone(F.normalize_airport("", None, alias=alias, iata_to_icao={}))


class RealSampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ix = idx(X.fixture_rows(), cao={"CA3155", "CA3156", "CA1005", "CA1006"},
                     search_index=X.load_json("search_index.json"))

    def test_foreign_callsign_rejected(self):
        # CA1303 history contains rows flown as CQH6567 / CSN3695 / ... under airline_icao CCA
        cs = {o.callsign for o in self.ix.obs}
        for bad in ("CQH6567", "CSN3695", "CXA8325", "CSZ8991"):
            self.assertNotIn(bad, cs)
        self.assertGreater(self.ix.dropped.get("non_target", 0), 0)
        self.assertTrue(any(o.callsign == "CCA1303" for o in self.ix.obs))
        self.assertEqual(F.attribute_row({"callsign": "CQH6567", "number": "CA1303", "airline_icao": "CCA"},
                                         self.ix.ctx), None)

    def test_cao_rows_without_number(self):
        # CAO1006 / CAO3155 continuation legs: callsign only (reg histories) -> keyed by callsign
        k = ("CAO", "CAO3155", "ESSA", "EBLG")
        self.assertIn(k, self.ix.by_key)
        self.assertTrue(any(o.number == "" for o in self.ix.by_key[k]))
        self.assertTrue(any(o.number == "" and o.source.startswith("reg:") for o in self.ix.by_key[("CAO", "CAO1006", "LEMD", "ZSPD")]))
        # callsign-first: number+OD index also finds number-less rows via the callsign-derived number
        self.assertIn(("CA3155", "ESSA", "EBLG"), self.ix.by_number_od)

    def test_alias_zsja_to_zsgs(self):
        aps = {o.orig for o in self.ix.obs} | {o.dest for o in self.ix.obs}
        self.assertNotIn("ZSJA", aps)
        self.assertIn("ZSGS", aps)

    def test_windows(self):
        cfg = S.cfg()
        self.assertTrue(all((o.window == "season") == (o.ev_ts >= cfg.season_start_ts) for o in self.ix.obs))
        self.assertTrue(all(k for k in self.ix.by_key))
        self.assertTrue(self.ix.pre_by_key)
        self.assertEqual(self.ix.recent_start_ts, self.ix.latest_ts - cfg.thresholds.recent_window_days * 86400)
        self.assertTrue(all(o.is_recent == (o.ev_ts >= self.ix.recent_start_ts) for o in self.ix.obs))
        self.assertIn("CA754", self.ix.fetched_numbers)
        self.assertNotIn("CA1109", self.ix.fetched_numbers)   # board rows only in the legacy cache


class AttributionTest(unittest.TestCase):
    def test_cao_placeholder(self):
        cfg = S.cfg()
        t = X.ts(2026, 11, 10, 8)
        sched = X.row("CA1234", "CCA1234", "ZSPD", "KORD", t, actual=False, airline="CCA")
        act = X.row("CA1234", "CCA1234", "ZSPD", "KORD", t + 86400, actual=True, airline="CCA")
        cargo_ac = X.row("CA999", "", "ZSPD", "KORD", t, actual=True, airline="", ac="B77L")
        ix = idx([sched, act, cargo_ac], cao={"CA1234"})
        by = {(o.sched_dep, o.callsign): o for o in ix.obs}
        o1 = by[(t, "CAO1234")]
        self.assertEqual((o1.op, o1.op_source), ("CAO", "cargo_fallback"))
        o2 = by[(t + 86400, "CCA1234")]          # actual rows keep their callsign (legacy is_cao_ph)
        self.assertEqual((o2.op, o2.op_source), ("CCA", "callsign"))
        o3 = by[(t, "CAO999")]
        self.assertEqual((o3.op, o3.op_source), ("CAO", "cargo_fallback"))

    def test_search_resolved_and_airline_fallback(self):
        cfg = S.cfg()
        t = X.ts(2026, 11, 10, 8)
        ctx = F.build_attribution_context(cfg, [X.row("CA8313", "BJN8313", "ZBAA", "ZSSS", t)], cao_numbers=())
        self.assertEqual(ctx.fn_auth["CA8313"], ("BJN", "BJN8313"))
        r = {"number": "CA8313", "callsign": "", "airline_icao": "CCA", "status_generic": "landed", "real_dep": t}
        self.assertEqual(F.attribute_row(r, ctx), ("BJN", "BJN8313", "CA8313", "search_resolved"))
        r2 = {"number": "CA7777", "callsign": "", "airline_icao": "CCA", "real_dep": t}
        self.assertEqual(F.attribute_row(r2, ctx), ("CCA", "CCA7777", "CA7777", "airline_fallback"))
        ctx2 = F.build_attribution_context(cfg, [], cao_numbers=(),
                                           search_index={"by_flight": {"CA8605": {"detail": {"operator": "BJN",
                                                                                              "callsign": "BJN8605"}}}})
        self.assertEqual(ctx2.fn_auth["CA8605"], ("BJN", "BJN8605"))
        self.assertIsNone(F.attribute_row({"number": "MU1", "callsign": "CES1", "airline_icao": "CES"}, ctx2))

    def test_identity_check(self):
        ops = ("CCA", "CAO", "CCD", "BJN")
        chk = lambda num, cs, sched=1: F.identity_check({"number": num, "callsign": cs, "sched_dep": sched}, ops)
        self.assertEqual(chk("CA1332", "CCA1332"), "ok")
        self.assertEqual(chk("CA51", "CCA051"), "ok")           # zero-padded callsign
        self.assertEqual(chk("CA123", "CCA123R"), "ok")         # letter suffix
        self.assertEqual(chk("CCD1536", "CCD1536"), "ok")
        self.assertEqual(chk("", "CAO3155"), "ok")              # CAO continuation legs without number
        self.assertEqual(chk("CA1564", "CCA1332"), "fix")       # schedule-matched: trust the number
        self.assertEqual(chk("CA1332", "CCA1394", None), "drop")  # live track only: nothing to trust
        self.assertEqual(chk("3U8101", "CCA1332", None), "drop")  # foreign number on our callsign

    def test_stale_callsign_b2043(self):
        # B-2043 kept broadcasting CCA1332 on CA1564 / CA1303 / CA1393 (FR24, 2026-11-08/09)
        t = X.ts(2026, 11, 8, 5)
        good = X.row("CA1332", "CCA1332", "ZGSZ", "ZBAA", t - 86400)
        stale = X.row("CA1303", "CCA1332", "ZBAA", "ZGSZ", t)                         # has schedule -> fixed
        junk = X.row("3U8101", "CCA1332", "ZBAA", "ZGSZ", t + 86400, sched=False)     # no schedule -> dropped
        ix = idx([good, stale, junk])
        keys = {(o.callsign, o.orig, o.dest) for o in ix.obs}
        self.assertIn(("CCA1332", "ZGSZ", "ZBAA"), keys)
        self.assertIn(("CCA1303", "ZBAA", "ZGSZ"), keys)
        self.assertNotIn(("CCA1332", "ZBAA", "ZGSZ"), keys)
        self.assertEqual(ix.dropped["identity_conflict"], 1)
        self.assertEqual(ix.dropped["identity_fixed(kept)"], 1)

    def test_filters_and_dedup(self):
        t = X.ts(2026, 11, 10, 8)
        a = X.row("CA1", "CCA1", "ZBAA", "ZSSS", t)
        dup_id = dict(a, source="board:ZBAA")
        canc = X.row("CA1", "CCA1", "ZBAA", "ZSSS", t + 86400, status="canceled")
        div = X.row("CA1", "CCA1", "ZBAA", "ZSSS", t + 2 * 86400, status="diverted")
        same = X.row("CA1", "CCA1", "ZBAA", "ZBAA", t + 3 * 86400)
        noid1 = X.row("CA1", "CCA1", "ZBAA", "ZSSS", t + 4 * 86400, fid=False)
        noid2 = dict(noid1, source="reg:B-1")
        nots = X.row("CA1", "CCA1", "ZBAA", "ZSSS", t, sched=False, actual=False)
        ix = idx([a, dup_id, canc, div, same, noid1, noid2, nots])
        self.assertEqual(len(ix.obs), 2)
        self.assertEqual(ix.dropped["canceled_diverted"], 2)
        self.assertEqual(ix.dropped["same_airport"], 1)
        self.assertEqual(ix.dropped["duplicate"], 2)
        self.assertEqual(ix.dropped["missing_ts"], 1)
        # board rows win over flight rows for the same id (legacy order live > board > flight > reg)
        self.assertTrue(any(o.source == "board:ZBAA" for o in ix.obs))

    def test_iata_only_rows(self):
        t = X.ts(2026, 11, 10, 8)
        r1 = X.row("CA1", "CCA1", "ZBAA", "ZSSS", t)
        r1["origin_iata"], r1["dest_iata"] = "PEK", "SHA"
        r2 = dict(X.row("CA1", "CCA1", None, None, t + 86400), origin_iata="PEK", dest_iata="SHA")
        ix = idx([r1, r2])
        self.assertEqual({(o.orig, o.dest) for o in ix.obs}, {("ZBAA", "ZSSS")})

    def test_live_rows(self):
        t = X.ts(2026, 11, 16, 8)
        live = {"id": "L1", "number": "CA935", "callsign": "CCA935", "airline_icao": "CCA", "origin_icao": None,
                "origin_iata": "PVG", "dest_icao": None, "dest_iata": "FRA", "status_live": True, "ev_ts": t,
                "source": "live"}
        ix = idx([live], iata_to_icao={"PVG": "ZSPD", "FRA": "EDDF"})
        o = ix.obs[0]
        self.assertEqual((o.op_source, o.is_actual, o.orig, o.dest, o.ev_ts), ("live", True, "ZSPD", "EDDF", t))


if __name__ == "__main__":
    unittest.main()
