"""Config loading/validation and models JSON round-trip."""

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import _support as S
from vcca import config as C
from vcca.models import (Confidence, Decision, DecisionCategory, EvidenceSummary, FieldChange, Reason, ReasonCode,
                         REASON_TEXT, RouteTarget, RunMeta, describe_reason, dump_decisions, load_decisions)

UTC = dt.timezone.utc


class ConfigTest(unittest.TestCase):
    def test_season_config(self):
        cfg = S.cfg()
        self.assertEqual(cfg.target_ops, ("CCA", "CAO", "CCD", "BJN"))
        self.assertEqual(cfg.start_date, dt.date(2026, 10, 25))
        self.assertEqual(cfg.end_date, dt.date(2027, 3, 27))
        self.assertEqual(cfg.season_start_ts, 1792857600)  # legacy WINTER_START_TS
        self.assertEqual(cfg.thresholds, C.Thresholds())   # season.toml repeats every default
        self.assertEqual(cfg.vamsys_db, Path(r"F:\vCCA\Data\workspaces.db"))
        self.assertEqual(cfg.cache_db, C.WORK_DIR / "fr24_cache" / "fr24_cache.db")
        self.assertEqual(cfg.airport_alias["ZSJA"], "ZSGS")
        self.assertEqual(cfg.callsign_to_number("CAO1234"), "CA1234")
        self.assertEqual(cfg.number_to_callsign("CCD", "CA1234"), "CCD1234")
        self.assertEqual(cfg.default_cost_index["CAO"], 150)

    def test_effective_time(self):
        cfg = S.cfg()
        self.assertEqual(cfg.effective_time(dt.datetime(2026, 11, 25, 3, 0, tzinfo=UTC)), "2026-11-25 16:00:00")
        p = cfg.new_run_dir(dt.datetime(2026, 11, 25, 7, 30, tzinfo=UTC))
        self.assertEqual(p, cfg.season_dir / "output" / "2026-11-25_0730")   # UTC

    def test_discovery(self):
        self.assertIn(S.cfg().season_dir, C.discover_seasons())

    def _tmp_season(self, text: str) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        sd = root / "2030-31-1"
        sd.mkdir()
        (sd / "season.toml").write_text(text, encoding="utf-8")
        for f in C.DEFAULTS["season"]["plan_files"]:
            (sd / f).write_bytes(b"")
        return root

    def test_validation_collects_errors(self):
        root = self._tmp_season(
            '[season]\nstart_date="2030-13-01"\nend_date="2031-03-27"\n'
            '[vamsys]\nworkspace_id="nope"\n'
            '[export]\neffective_time="tomorrow"\ndefault_cost_index={CCA=80}\n'
            '[thresholds]\nbank_merge=[[45]]\nrecent_window_days=-3\nfoo=1\n')
        with self.assertRaises(C.ConfigError) as cm:
            C.load_config(plan_root=root, strict=False)
        msg = "\n".join(cm.exception.errors)
        for needle in ("start_date", "workspace_id", "effective_time", "default_cost_index", "bank_merge",
                       "recent_window_days"):
            self.assertIn(needle, msg)

    def test_defaults_and_fixed_effective_time(self):
        root = self._tmp_season(
            '[season]\nstart_date="2030-10-27"\nend_date="2031-03-29"\n'
            '[vamsys]\nworkspace_id="b8d41af1-5641-4b06-a476-b39829a63d25"\ndb_path="F:/vCCA/Data/workspaces.db"\n'
            '[export]\neffective_time="2030-11-01 16:00"\ndefault_cost_index={CCA=80,CAO=150,CCD=80,BJN=80}\n'
            '[thresholds]\nplan_attach=[30, 40]\n')
        cfg = C.load_config(plan_root=root, strict=False)
        self.assertEqual(cfg.effective_time(), "2030-11-01 16:00:00")
        self.assertEqual(cfg.thresholds.plan_attach, (30, 40))
        self.assertEqual(cfg.thresholds.bank_merge, ((45, 60), (20, 75), (65, 20)))
        self.assertEqual(cfg.season_label, "2030-31-1")
        self.assertEqual(cfg.plan_groups, ("国内", "国际", "港澳"))
        self.assertEqual(cfg.fetch.rate_per_sec, 2.0)

    def test_missing_season(self):
        with self.assertRaises(C.ConfigError):
            C.load_config("does-not-exist")

    def test_credentials_never_expose_values(self):
        st = C.credential_status(S.cfg())
        for path, hu, hp in st["checked"]:
            self.assertIsInstance(hu, bool)
            self.assertIsInstance(hp, bool)


class ModelsTest(unittest.TestCase):
    def test_every_reason_code_has_text(self):
        self.assertEqual(set(ReasonCode) - set(REASON_TEXT), set())

    def test_describe_reason(self):
        self.assertEqual(describe_reason(Reason(ReasonCode.WEEKDAY_ADDED, {"old": "1.3....", "new": "1.3.5.."})),
                         "按FR24实飞增补星期（1.3....→1.3.5..）")
        self.assertEqual(describe_reason(Reason(ReasonCode.AIRPORT_MISSING, {})), "VAMSYS缺场?")
        self.assertEqual(describe_reason(Reason("X_NEW", {"a": 1})), "X_NEW(a=1)")
        self.assertEqual(describe_reason({"code": "FLEET_EXTRA", "params": {"fleets": ["A321", "B752"]}}),
                         "含超飞行区等级或跨宽窄机型（A321,B752）")

    def test_decisions_json_roundtrip(self):
        tgt = RouteTarget(op="CCA", callsign="CCA1109", number="CA1109", orig="ZBAA", dest="ZBXH", dep="02:55",
                          arr="04:20", days=frozenset({0, 1, 2, 3, 4, 5, 6}), fleet_ids=("26071", "26102"),
                          cost_index=80)
        cur = RouteTarget(op="CCA", callsign="CCA1109", number="CA1109", orig="ZBAA", dest="ZBXH", dep="02:40",
                          arr="04:05", days=frozenset({6}), fleet_ids=("26102",), cost_index=80)
        d = Decision(category=DecisionCategory.UPDATE, target=tgt, vroute_id="11507830", current=cur,
                     changes=[FieldChange("days", [6], [0, 1, 2, 3, 4, 5, 6])],
                     evidence=EvidenceSummary(state="active", acts=40, banks=[{"dep": "02:55", "arr": "04:20"}]),
                     confidence=Confidence.HIGH, group_key=("CCA", "CCA1109", "ZBAA", "ZBXH"),
                     details={"weekday_acts": {0: 5}})
        d.add_reason(ReasonCode.MICRO_SPLIT_MERGED, n=2)
        d.add_reason(ReasonCode.WEEKDAY_DROPPED, review=True, weeks=4, old="1234567", new="123456.")
        self.assertTrue(d.needs_review)
        self.assertEqual(d.csv_file, "03_routes_update")
        meta = RunMeta(season="2026-27-1", season_label="x", generated_at="2026-11-25 00:00:00",
                       effective_time="2026-11-25 16:00:00", season_start="2026-10-25", season_end="2027-03-27",
                       counts={"UPDATE": 1})
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = dump_decisions(tmp / "decisions.json", meta, [d])
        meta2, (d2,) = load_decisions(p)
        self.assertEqual(meta2, meta)
        self.assertEqual(d2.target, d.target)
        self.assertEqual(d2.current, d.current)
        self.assertEqual(d2.reasons, d.reasons)
        self.assertEqual(d2.changes, d.changes)
        self.assertEqual(d2.group_key, d.group_key)
        self.assertEqual(d2.category, DecisionCategory.UPDATE)
        self.assertEqual(d2.review_flags, ["WEEKDAY_DROPPED"])
        self.assertEqual(d2.to_dict(), {**d.to_dict(), "details": {"weekday_acts": {"0": 5}}})


if __name__ == "__main__":
    unittest.main()
