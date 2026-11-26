"""fr24_store: upsert/accumulation, filters, targets, legacy import counts (fixtures cut from the real cache)."""

import json
import tempfile
import unittest
from pathlib import Path

from tests import _support as S  # noqa: F401  (sys.path)
from vcca import fr24_store as FS
from vcca.models import Priority, TargetKind, TargetState

FIX = Path(__file__).resolve().parent / "fixtures" / "fetch"


def _row(i="a1", **kw):
    r = {k: None for k in ("id", "number", "callsign", "airline_icao", "origin_icao", "origin_iata", "dest_icao",
                           "dest_iata", "aircraft_code", "registration", "sched_dep", "sched_arr", "real_dep",
                           "real_arr", "status_live", "status_text", "status_generic")}
    r.update(id=i, number="CA1109", callsign="CCA1109", origin_icao="ZBAA", dest_icao="ZBXH",
             sched_dep=1793000000, sched_arr=1793005000)
    r.update(kw)
    return r


class _TmpStore(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.st = FS.FR24Store(Path(self._d.name) / "c.db")

    def tearDown(self):
        self.st.close()
        self._d.cleanup()


class UpsertTest(_TmpStore):
    def test_insert_update_unchanged_and_accumulate(self):
        s = self.st.upsert_rows([_row("a1"), _row("a2", sched_dep=1793086400)], source="flight:CA1109", seen_at=100)
        self.assertEqual((s.inserted, s.updated, s.unchanged), (2, 0, 0))
        # newer fetch: real times arrive, a2 not returned any more -> stays (history accumulates)
        s = self.st.upsert_rows([_row("a1", real_dep=1793000300, status_generic="Landed")], source="reg:B-1", seen_at=200)
        self.assertEqual((s.inserted, s.updated), (0, 1))
        s = self.st.upsert_rows([_row("a1", real_dep=1793000300, status_generic="landed")], source="reg:B-1", seen_at=300)
        self.assertEqual(s.unchanged, 1)
        self.assertEqual(self.st.count_rows(), 2)
        r = next(self.st.iter_rows(number="CA1109", since_ts=1793000000 - 1))
        self.assertEqual((r["id"], r["real_dep"], r["status_generic"]), ("a1", 1793000300, "landed"))
        self.assertEqual((r["first_seen"], r["last_seen"], r["source"]), (100, 300, "reg:B-1"))

    def test_older_observation_only_fills_nulls(self):
        self.st.upsert_rows([_row("a1", aircraft_code="A321", status_live=False)], source="new", seen_at=500)
        self.st.upsert_rows([_row("a1", aircraft_code="A320", registration="B-1", status_live=True)], source="old", seen_at=100)
        r = next(self.st.iter_rows())
        self.assertEqual((r["aircraft_code"], r["registration"], r["status_live"]), ("A321", "B-1", False))
        self.assertEqual((r["first_seen"], r["last_seen"], r["source"]), (100, 500, "new"))

    def test_newer_null_does_not_erase(self):
        self.st.upsert_rows([_row("a1", registration="B-1")], source="s", seen_at=1)
        self.st.upsert_rows([_row("a1", registration="")], source="s", seen_at=2)
        self.assertEqual(next(self.st.iter_rows())["registration"], "B-1")

    def test_synthetic_id(self):
        r = _row(None)
        sid = FS.synthetic_id(r)
        self.assertTrue(sid.startswith("nid:") and len(sid) == 20)
        self.assertEqual(sid, FS.synthetic_id(dict(r, callsign="cca1109")))      # case-insensitive
        self.assertNotEqual(sid, FS.synthetic_id(dict(r, sched_dep=1)))
        self.st.upsert_rows([r, dict(r)], source="s", seen_at=1)
        self.assertEqual([x["id"] for x in self.st.iter_rows()], [sid])

    def test_filters(self):
        self.st.upsert_rows([_row("a1", registration="B-1"), _row("a2", callsign="CAO1109", registration="B-2",
                                                                 sched_dep=10, real_dep=None)], source="s", seen_at=1)
        self.assertEqual([r["id"] for r in self.st.iter_rows(reg="b-2")], ["a2"])
        self.assertEqual([r["id"] for r in self.st.iter_rows(callsign="CCA1109")], ["a1"])
        self.assertEqual([r["id"] for r in self.st.iter_rows(since_ts=1000)], ["a1"])
        self.assertEqual(len(list(self.st.iter_rows(number="CA1109", reg="B-1"))), 1)

    def test_iata_to_icao(self):
        self.st.upsert_rows([_row("a1", origin_iata="PEK", dest_iata="XIL")], source="s", seen_at=1)
        self.st.upsert_airport_info("ZSJA", "JGS", "Jinggangshan")
        m = self.st.iata_to_icao()
        self.assertEqual((m["PEK"], m["XIL"], m["JGS"]), ("ZBAA", "ZBXH", "ZSJA"))
        self.assertEqual(self.st.airport_info()["ZSJA"], ("JGS", "Jinggangshan"))


class TargetsLogMetaTest(_TmpStore):
    def test_targets_roundtrip(self):
        n = self.st.upsert_targets([TargetState("CA1", TargetKind.FLIGHT, Priority.P1, 5, "active", "x"),
                                    TargetState("B-1", TargetKind.REG)])
        self.assertEqual(n, 2)
        t = self.st.get_target("CA1", TargetKind.FLIGHT)
        self.assertEqual((t.priority, t.last_ok_at, t.last_state, t.kind), (Priority.P1, 5, "active", TargetKind.FLIGHT))
        self.assertEqual([t.query for t in self.st.iter_targets(TargetKind.REG)], ["B-1"])
        self.st.upsert_targets([TargetState("CA1", TargetKind.FLIGHT, Priority.P2, 9)])
        self.assertEqual(self.st.get_target("CA1", TargetKind.FLIGHT).priority, Priority.P2)
        # build writes tiers without fetch times: last_ok_at must survive, other columns update
        self.st.upsert_targets([TargetState("CA1", TargetKind.FLIGHT, Priority.P1, None, "sched_only", "WATCH_PLAN")])
        t = self.st.get_target("CA1", TargetKind.FLIGHT)
        self.assertEqual((t.priority, t.last_ok_at, t.last_state, t.reason), (Priority.P1, 9, "sched_only", "WATCH_PLAN"))
        self.assertEqual(self.st.delete_targets([("B-1", TargetKind.REG)]), 1)
        self.assertIsNone(self.st.get_target("B-1", TargetKind.REG))

    def test_fetch_log_and_stats(self):
        self.st.log_fetch("CA1", "flight", 100, 200, 3)
        self.st.log_fetch("CA1", "flight", 200, 500, 0, "HTTP 500")
        self.assertEqual(self.st.last_fetch("CA1", "flight")["error"], "HTTP 500")
        lf = self.st.last_fetches()[("CA1", "flight")]
        self.assertEqual((lf["last_ok_at"], lf["error"]), (100, "HTTP 500"))
        self.st.upsert_targets([TargetState("CA1", TargetKind.FLIGHT, Priority.P2, 100)])
        s = self.st.stats()
        self.assertEqual((s.last_fetch_at, s.fetches_by_kind, s.targets_by_priority, s.stale_targets),
                         (100, {"flight": 2}, {"P2": 1}, 1))
        self.st.set_meta("k", "v")
        self.assertEqual(self.st.get_meta("k"), "v")

    def test_readonly(self):
        self.st.upsert_rows([_row()], source="s", seen_at=1)
        self.st.close()
        ro = FS.FR24Store(Path(self._d.name) / "c.db", readonly=True)
        try:
            self.assertEqual(ro.count_rows(), 1)
            with self.assertRaises(Exception):
                ro.set_meta("x", "y")
        finally:
            ro.close()
        self.st = FS.FR24Store(Path(self._d.name) / "c.db")


def _expected_legacy_ids():
    """Independent count of the fixture rows: every (id or synthetic id) per source."""
    ids, n = set(), 0
    for name in ("flight_histories.json", "reg_histories.json"):
        for p in json.loads((FIX / name).read_text(encoding="utf-8"))["flights"].values():
            for r in p["rows"]:
                n += 1
                ids.add(r.get("id") or FS.synthetic_id(r))
    for path in (FIX / "airports").glob("*.json"):
        for r in json.loads(path.read_text(encoding="utf-8"))["rows"]:
            n += 1
            ids.add(r.get("id") or FS.synthetic_id(r))
    live = json.loads((FIX / "live_summary.json").read_text(encoding="utf-8"))
    for fls in live["live"].values():
        for f in fls:
            n += 1
            ids.add(f["id"])
    for fid, d in live["details"].items():
        if not any(f["id"] == fid for fls in live["live"].values() for f in fls):
            n += 1
            ids.add(fid)
        for h in d.get("history") or []:
            n += 1
            ids.add(h["id"])
    return ids, n


class LegacyImportTest(_TmpStore):
    def test_import_counts_no_loss_idempotent(self):
        rep = self.st.import_legacy_json(FIX)
        ids, n = _expected_legacy_ids()
        self.assertEqual(rep.errors, [])
        self.assertEqual((rep.flights, rep.regs, rep.boards, rep.airports), (3, 1, 1, 1))
        self.assertEqual(rep.rows_in, n)
        self.assertEqual(rep.distinct_ids, len(ids))
        self.assertEqual(rep.duplicates, n - len(ids))
        self.assertGreaterEqual(rep.duplicates, 1)                 # reg payload repeats a flight-history row
        self.assertGreater(rep.rows_without_id, 0)
        self.assertEqual(rep.missing_after_import, 0)
        self.assertEqual(self.st.count_rows(), len(ids))
        self.assertEqual(set(r["id"] for r in self.st.iter_rows()), ids)
        self.assertEqual(sum(rep.rows_by_source.values()), n)
        self.assertEqual(rep.legacy_errors, 1)
        self.assertEqual(self.st.last_fetch("CA9999", "flight")["http_status"], None)
        self.assertIsNotNone(self.st.last_fetch("CA1002", "flight"))
        self.assertIsNotNone(self.st.last_fetch("ZBAA", "board"))
        # search index rebuilt identically
        legacy_si = json.loads((FIX / "search_index.json").read_text(encoding="utf-8"))
        si = self.st.search_index()
        self.assertEqual(set(si["by_flight"]), set(legacy_si["by_flight"]))
        self.assertEqual(set(si["by_callsign"]), set(legacy_si["by_callsign"]))
        # idempotent
        log_n = self.st.conn.execute("SELECT count(*) FROM fetch_log").fetchone()[0]
        self.assertTrue(self.st.import_legacy_json(FIX).already_done)
        rep2 = self.st.import_legacy_json(FIX, force=True)
        self.assertEqual((rep2.rows_inserted, rep2.rows_updated), (0, 0))
        self.assertEqual(self.st.count_rows(), len(ids))
        self.assertEqual(self.st.conn.execute("SELECT count(*) FROM fetch_log").fetchone()[0], log_n)

    def test_live_rows_shape(self):
        self.st.import_legacy_json(FIX)
        live = [r for r in self.st.iter_rows() if r["source"] == "live" and r["status_live"]]
        self.assertTrue(live)
        self.assertTrue(all(r["airline_icao"] for r in live))


if __name__ == "__main__":
    unittest.main()
