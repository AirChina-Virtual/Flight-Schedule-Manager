"""fr24_fetch: target derivation, tier planning, token bucket, run loop failure/abort/resume (fake client)."""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import _support as S
from vcca import fr24_fetch as FF
from vcca import fr24_store as FS
from vcca.fr24_client import FetchResult, FR24AuthError, FR24Error, FR24RateLimited
from vcca.models import Priority, TargetKind, TargetState

H = 3600
NOW = 1795000000              # 2026-11-18, inside the season
SEASON_TS = 1793000000        # a flight inside the season


def _cfg(**fetch_kw):
    c = S.cfg()
    return dataclasses.replace(c, fetch=dataclasses.replace(c.fetch, **fetch_kw)) if fetch_kw else c


def _row(i, number="CA1109", callsign="CCA1109", reg="B-1", ts=SEASON_TS, actual=True):
    return {"id": i, "number": number, "callsign": callsign, "airline_icao": callsign[:3] if callsign else "CCA",
            "origin_icao": "ZBAA", "origin_iata": None, "dest_icao": "ZBXH", "dest_iata": None,
            "aircraft_code": "A321", "registration": reg, "sched_dep": ts, "sched_arr": ts + 5000,
            "real_dep": ts + 300 if actual else None, "real_arr": None, "status_live": False,
            "status_text": "", "status_generic": "landed" if actual else "scheduled"}


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeClient:
    """script: query -> list of outcomes (Exception instance or list of rows); default = one row."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def _next(self, q):
        self.calls.append(q)
        seq = self.script.get(q)
        out = seq.pop(0) if seq else [_row(f"id-{q}", number=q if q.startswith("CA") else "CA1")]
        if isinstance(out, BaseException):
            raise out
        return out

    def flight_list(self, query, kind="flight", *, page=1, limit=100):
        return FetchResult(query, kind, 200, rows=self._next(query), fetched_at=NOW)

    def airport_board(self, icao, *, pages=1):
        return FetchResult(icao, "board", 200, rows=self._next(icao), fetched_at=NOW)

    def search(self, query, *, limit=50):
        self._next(query)
        return {"schedule": [{"id": query + "1", "detail": {"flight": query + "1", "callsign": "CCA", "operator": "CCA"}}],
                "live": []}


def _instant_bucket():
    fc = FakeClock()
    return FF.TokenBucket(1000.0, clock=fc.clock, sleep=fc.sleep)


class _TmpStore(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.st = FS.FR24Store(Path(self._d.name) / "c.db")
        self.logs = []

    def tearDown(self):
        self.st.close()
        self._d.cleanup()

    def run_plan(self, cfg, client, items, deep=False):
        fp = FF.FetchPlan(items=items, deep=deep)
        return FF.run_fetch(cfg, self.st, client, fp, log=self.logs.append, progress=lambda *a: None,
                            bucket=_instant_bucket(), now_fn=lambda: NOW)


class TokenBucketTest(unittest.TestCase):
    def test_rate_spacing(self):
        fc = FakeClock()
        b = FF.TokenBucket(2.0, clock=fc.clock, sleep=fc.sleep)
        for _ in range(5):
            b.acquire()
        self.assertAlmostEqual(fc.t, 2.0, places=6)         # first is free, then 4 x 0.5 s

    def test_halve_backoff_and_recover(self):
        fc = FakeClock()
        b = FF.TokenBucket(2.0, clock=fc.clock, sleep=fc.sleep, recover_every=4, backoff_s=10)
        b.acquire()
        b.on_rate_limited()
        self.assertEqual(b.rate, 1.0)
        t0 = fc.t
        b.acquire()
        self.assertGreaterEqual(fc.t - t0, 10.0)            # backoff honoured
        b.on_rate_limited(retry_after=3)
        self.assertEqual(b.rate, 0.5)
        for _ in range(4):
            b.on_success()
        self.assertAlmostEqual(b.rate, 0.625)               # x1.25 after recover_every successes
        for _ in range(100):
            b.on_success()
        self.assertEqual(b.rate, 2.0)                       # capped at base
        with self.assertRaises(ValueError):
            FF.TokenBucket(0)


class PlanFetchTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def test_tiers_ttl_order(self):
        T = TargetState
        ts = [
            T("CA9", TargetKind.FLIGHT, Priority.P2, NOW - 200 * H),          # P2 stale (168 h)
            T("CA8", TargetKind.FLIGHT, Priority.P2, NOW - 10 * H),           # P2 fresh
            T("CA7", TargetKind.FLIGHT, Priority.P1, NOW - 30 * H),           # P1 stale (24 h)
            T("CA6", TargetKind.FLIGHT, Priority.P1, NOW - 2 * H),            # P1 fresh
            T("CA5", TargetKind.FLIGHT, Priority.P0, None),                   # new
            T("CA4", TargetKind.FLIGHT, Priority.P0, NOW - 1 * H),            # last failed
            T("B-C", TargetKind.REG, Priority.P1, NOW - H, reason="cao"),     # CAO reg: every run
            T("B-F", TargetKind.REG, Priority.P1, NOW - H, reason="fleet"),   # not deep -> skip
            T("B-N", TargetKind.REG, Priority.P0, None, reason="fleet"),      # never fetched -> due
        ]
        fp = FF.plan_fetch(self.cfg, ts, now=NOW, last_deep_scan_at=NOW - 2 * 86400)
        self.assertFalse(fp.deep)
        got = [(i.query, str(i.priority), i.reason) for i in fp.items]
        self.assertEqual(got, [("CA4", "P0", "failed"), ("CA5", "P0", "new"), ("B-N", "P0", "new"),
                               ("CA7", "P1", "ttl_p1"), ("B-C", "P1", "cao"), ("CA9", "P2", "ttl_p2")])
        self.assertEqual(fp.skipped_fresh, 3)
        self.assertEqual(fp.est_requests, 6)
        self.assertEqual(fp.by_priority(), {"P0": 3, "P1": 2, "P2": 1})
        self.assertEqual(fp.by_kind(), {"flight": 4, "reg": 2})
        # deep (flag or overdue scan) -> every reg
        for kw in ({"deep": True, "last_deep_scan_at": NOW}, {"last_deep_scan_at": NOW - 8 * 86400},
                   {"last_deep_scan_at": None}):
            fp = FF.plan_fetch(self.cfg, ts, now=NOW, **kw)
            self.assertTrue(fp.deep)
            self.assertEqual(sorted(i.query for i in fp.items if i.kind == TargetKind.REG), ["B-C", "B-F", "B-N"])
        # explicit cao_regs overrides reason
        fp = FF.plan_fetch(self.cfg, ts, now=NOW, last_deep_scan_at=NOW, cao_regs=["B-F"])
        self.assertEqual(sorted(i.query for i in fp.items if i.kind == TargetKind.REG), ["B-F", "B-N"])

    def test_explicit_and_search(self):
        ts = [TargetState("CA1109", TargetKind.FLIGHT, Priority.P0), TargetState("CA1110", TargetKind.FLIGHT)]
        fp = FF.plan_fetch(self.cfg, ts, now=NOW, flights=["ca1", "CA1"], regs=["b-1"], boards=["zbaa"], search=True)
        self.assertEqual([(str(i.kind), i.query, i.reason) for i in fp.items],
                         [("flight", "CA1", "forced"), ("reg", "B-1", "forced"), ("board", "ZBAA", "forced"),
                          ("search", "CA110", "search"), ("search", "CA111", "search")])
        self.assertEqual(FF.search_prefixes(["CA1109", "CA1110", "CA1201", "BJN8313"]), ["BJN83", "CA110", "CA111", "CA120"])


class DeriveTargetsTest(unittest.TestCase):
    def test_sources_merge_and_drop(self):
        cfg = _cfg()
        with tempfile.TemporaryDirectory() as d:
            st = FS.FR24Store(Path(d) / "c.db")
            try:
                st.upsert_rows([
                    _row("r1", "CA1109", "CCA1109", "B-1", ts=NOW - 2 * 86400),
                    _row("r2", "CA1043", "CAO1043", "B-2", ts=NOW - 3 * 86400),             # CAO -> cao reg
                    _row("r3", "CA9001", "CCA9001", "B-3", ts=NOW - 40 * 86400 + 86400 * 2),  # >30 d (if in season)
                    _row("r4", "CA1303", "CQH6567", "B-9", ts=NOW - 86400),                 # other op: ignored
                    _row("r5", "CA7777", "CCA7777", "B-4", ts=NOW - 86400, actual=False),   # sched only: no reg
                    _row("r6", "CA5555", "CCA5555", "B-5", ts=SEASON_TS - 86400 * 30),      # pre-season: ignored
                ], source="reg:x", seen_at=NOW)
                st.log_fetch("CA1109", "flight", NOW - 5 * H, 200, 1)
                st.upsert_targets([TargetState("CA2000", TargetKind.FLIGHT, Priority.P2, NOW - H, "active", "plan")])
                plan = SimpleNamespace(numbers=frozenset({"CA1109", "CA2000"}))
                vs = SimpleNamespace(commercial_routes=[
                    SimpleNamespace(op="CCA", number="CA3000", callsign="CCA3000"),
                    SimpleNamespace(op="CCA", number="", callsign="CCA3001"),
                    SimpleNamespace(op="CES", number="MU1", callsign="CES1"),
                ])
                ts = {(t.query, str(t.kind)): t for t in FF.derive_targets(cfg, plan, vs, st, now=NOW)}
                flights = {q: t for (q, k), t in ts.items() if k == "flight"}
                regs = {q: t for (q, k), t in ts.items() if k == "reg"}
                self.assertEqual(flights["CA1109"].reason, "plan")
                self.assertEqual((flights["CA1109"].priority, flights["CA1109"].last_ok_at), (Priority.P1, NOW - 5 * H))
                self.assertEqual((flights["CA2000"].priority, flights["CA2000"].last_state), (Priority.P2, "active"))
                self.assertEqual((flights["CA3000"].reason, flights["CA3001"].reason), ("vamsys", "vamsys"))
                self.assertEqual(flights["CA3000"].priority, Priority.P0)
                self.assertEqual(flights["CA1043"].reason, "discovered")
                self.assertEqual(flights["CA7777"].reason, "discovered")        # scheduled rows still name numbers
                self.assertNotIn("MU1", flights)
                self.assertNotIn("CA1303", flights)
                self.assertNotIn("CA5555", flights)
                self.assertEqual({q: t.reason for q, t in regs.items()}, {"B-1": "fleet", "B-2": "cao"})
                # stale reg target in the store is reported for deletion
                st.upsert_targets([TargetState("B-OLD", TargetKind.REG, Priority.P1, NOW - H, reason="fleet")])
                self.assertEqual(FF.stale_reg_targets(st, list(ts.values())), [("B-OLD", TargetKind.REG)])
            finally:
                st.close()


class RunFetchTest(_TmpStore):
    def items(self, *qs, kind=TargetKind.FLIGHT):
        return [FF.FetchItem(q, kind, Priority.P0, "new") for q in qs]

    def test_success_persists_rows_log_targets(self):
        cl = FakeClient({"B-7": [[_row("x1", "CA4321", "CCA4321", "B-7"), _row("x2", "CA1109", "CCA1109", "B-8")]]})
        st0 = TargetState("CA1109", TargetKind.FLIGHT, Priority.P2, NOW - 999 * H, "active", "plan")
        self.st.upsert_targets([st0])
        rep = self.run_plan(_cfg(), cl, self.items("CA1109") + self.items("B-7", kind=TargetKind.REG), deep=True)
        self.assertEqual((rep.ok, rep.failed, rep.stopped_early, rep.requests), (2, 0, False, 2))
        self.assertEqual(self.st.count_rows(), 3)
        t = self.st.get_target("CA1109", TargetKind.FLIGHT)
        self.assertEqual((t.priority, t.last_ok_at, t.last_state), (Priority.P2, NOW, "active"))   # tier kept
        t = self.st.get_target("B-7", TargetKind.REG)
        self.assertEqual((t.priority, t.last_ok_at), (Priority.P1, NOW))                          # new -> P1
        self.assertEqual(rep.new_numbers, ["CA4321"])
        self.assertEqual(rep.new_regs, ["B-1", "B-8"])           # B-1 from the CA1109 default row
        self.assertEqual(self.st.last_fetch("B-7", "reg")["rows"], 2)
        self.assertEqual(self.st.get_meta(FF.META_LAST_DEEP), str(NOW))

    def test_rate_limited_retry_then_ok(self):
        cl = FakeClient({"CA1": [FR24RateLimited("429"), FR24RateLimited("429"), [_row("a")]]})
        rep = self.run_plan(_cfg(), cl, self.items("CA1"))
        self.assertEqual((rep.ok, rep.failed, rep.rate_limited, rep.requests), (1, 0, 2, 3))

    def test_rate_limited_exhausted_is_failure(self):
        cl = FakeClient({"CA1": [FR24RateLimited("429")] * (FF.RATE_LIMIT_RETRIES + 1)})
        rep = self.run_plan(_cfg(), cl, self.items("CA1", "CA2"))
        self.assertEqual((rep.ok, rep.failed), (1, 1))
        self.assertEqual(self.st.get_target("CA1", TargetKind.FLIGHT).priority, Priority.P0)

    def test_abort_after_consecutive_failures_and_resume(self):
        cfg = _cfg(max_consecutive_failures=3)
        fail = {q: [FR24Error("HTTP 500", 500)] for q in ("CA2", "CA3", "CA4")}
        cl = FakeClient(fail)
        rep = self.run_plan(cfg, cl, self.items("CA1", "CA2", "CA3", "CA4", "CA5"), deep=True)
        self.assertTrue(rep.stopped_early)
        self.assertEqual((rep.ok, rep.failed, rep.done), (1, 3, 4))
        self.assertNotIn("CA5", cl.calls)
        self.assertIsNone(self.st.get_meta(FF.META_LAST_DEEP))          # incomplete deep scan not recorded
        self.assertEqual(self.st.last_fetch("CA2", "flight")["http_status"], 500)
        # resume: plan again from the stored targets -> only failed + never-fetched are due
        targets = list(self.st.iter_targets()) + [TargetState("CA5", TargetKind.FLIGHT)]
        fp = FF.plan_fetch(cfg, targets, now=NOW + 60, last_deep_scan_at=NOW)
        self.assertEqual([(i.query, i.reason) for i in fp.items],
                         [("CA2", "new"), ("CA3", "new"), ("CA4", "new"), ("CA5", "new")])
        rep2 = self.run_plan(cfg, FakeClient(), fp.items)
        self.assertEqual((rep2.ok, rep2.failed), (4, 0))
        fp = FF.plan_fetch(cfg, list(self.st.iter_targets()), now=NOW + 120, last_deep_scan_at=NOW)
        self.assertEqual(fp.items, [])

    def test_auth_error_stops(self):
        cl = FakeClient({"CA2": [FR24AuthError("HTTP 403", 403)]})
        rep = self.run_plan(_cfg(), cl, self.items("CA1", "CA2", "CA3"))
        self.assertTrue(rep.stopped_early)
        self.assertTrue(rep.stop_reason.startswith("auth"))
        self.assertEqual(cl.calls, ["CA1", "CA2"])

    def test_interrupt_keeps_progress(self):
        cl = FakeClient({"CA2": [KeyboardInterrupt()]})
        rep = self.run_plan(_cfg(), cl, self.items("CA1", "CA2", "CA3"))
        self.assertEqual((rep.stopped_early, rep.stop_reason, rep.ok), (True, "interrupted", 1))
        self.assertEqual(self.st.get_target("CA1", TargetKind.FLIGHT).last_ok_at, NOW)

    def test_board_and_search(self):
        items = [FF.FetchItem("ZBAA", TargetKind.BOARD, Priority.P0, "forced"),
                 FF.FetchItem("CA110", FF.SEARCH_KIND, Priority.P2, "search")]
        rep = self.run_plan(_cfg(), FakeClient(), items)
        self.assertEqual(rep.ok, 2)
        self.assertIn("CA1101", self.st.search_index()["by_flight"])
        self.assertIsNotNone(self.st.get_target("ZBAA", TargetKind.BOARD))


class FetchEntryDryRunTest(unittest.TestCase):
    """fetch(dry_run=True) against a temp cache dir: imports fixtures once, sends nothing, writes no targets."""

    def test_dry_run(self):
        import shutil

        fix = Path(__file__).resolve().parent / "fixtures" / "fetch"
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(fix, Path(d) / "cache")
            cfg = dataclasses.replace(S.cfg(), cache_dir=Path(d) / "cache")

            class NoClient:
                def __getattr__(self, name):
                    raise AssertionError("dry-run must not touch the client")

            logs = []
            rep = FF.fetch(cfg, dry_run=True, log=logs.append, client=NoClient())
            self.assertTrue(rep.dry_run)
            self.assertGreaterEqual(rep.planned, len(S.plan().numbers))
            with FS.FR24Store(cfg.cache_db) as st:
                self.assertIsNotNone(st.get_meta("legacy_import_done_at"))
                self.assertEqual(list(st.iter_targets()), [])
            rep = FF.fetch(cfg, dry_run=True, log=logs.append, flights=["CA1109"], client=NoClient())
            self.assertEqual(rep.planned, 1)


if __name__ == "__main__":
    unittest.main()
