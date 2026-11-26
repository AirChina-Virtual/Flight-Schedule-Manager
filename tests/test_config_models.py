"""Config loading/validation and models JSON round-trip."""

import datetime as dt
import json
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests import _support as S
from vcca import config as C
from vcca.models import (Confidence, Decision, DecisionCategory, EvidenceSummary, FieldChange, Reason, ReasonCode,
                         REASON_TEXT, RouteTarget, RunMeta, describe_reason, dump_decisions, load_decisions)

UTC = dt.timezone.utc


WS = "b8d41af1-5641-4b06-a476-b39829a63d25"
CI = '[export]\ndefault_cost_index = { CCA = 80, CAO = 150, CCD = 80, BJN = 80 }\n'


class ConfigTest(unittest.TestCase):
    def test_season_config(self):
        cfg = S.cfg()
        self.assertEqual(cfg.target_ops, ("CCA", "CAO", "CCD", "BJN"))
        self.assertEqual(cfg.start_date, dt.date(2026, 10, 25))
        self.assertEqual(cfg.end_date, dt.date(2027, 3, 27))
        self.assertEqual(cfg.season_start_ts, 1792857600)  # former WINTER_START_TS
        self.assertEqual(cfg.thresholds, C.Thresholds())   # no threshold differs from the code defaults
        self.assertEqual(cfg.vamsys_db, C.vcca_home() / "vamsys" / "workspaces.db")
        self.assertEqual(cfg.cache_db, C.vcca_home() / "cache" / "fr24_cache.db")
        self.assertEqual(cfg.season_dir.parent, C.vcca_home() / "seasons")
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

    # ---- temp data directories
    def _home(self, season_text: str | None, settings_text: str | None = None, *, xls: bool = True) -> Path:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        shutil.copytree(S.RULES_TEMPLATE, home / "rules")
        sd = home / "seasons" / "2030-31-1"
        sd.mkdir(parents=True)
        if season_text is not None:
            (sd / "season.toml").write_text(season_text, encoding="utf-8")
        if settings_text is not None:
            (home / "settings.toml").write_text(settings_text, encoding="utf-8")
        if xls:
            for f in C.DEFAULTS["season"]["plan_files"]:
                (sd / f).write_bytes(b"")
        return home

    def test_validation_collects_errors(self):
        home = self._home(
            '[season]\nstart_date="2030-13-01"\nend_date="2031-03-27"\n'
            '[vamsys]\nworkspace_id="nope"\n'
            '[export]\neffective_time="tomorrow"\ndefault_cost_index={CCA=80}\n'
            '[thresholds]\nbank_merge=[[45]]\nrecent_window_days=-3\nfoo=1\n')
        with self.assertRaises(C.ConfigError) as cm:
            C.load_config(home=home, strict=False)
        msg = "\n".join(cm.exception.errors)
        for needle in ("start_date", "workspace_id", "effective_time", "default_cost_index", "bank_merge",
                       "recent_window_days"):
            self.assertIn(needle, msg)

    def test_missing_workspace_points_to_data_page(self):
        home = self._home('[season]\nstart_date="2030-10-27"\nend_date="2031-03-29"\n', CI)
        with self.assertRaises(C.ConfigError) as cm:
            C.load_config(home=home, strict=False)
        self.assertTrue(any("请在数据页上传 workspaces.db 并选择工作区" in e for e in cm.exception.errors))

    def test_defaults_layers_and_fixed_effective_time(self):
        home = self._home(
            '[season]\nstart_date="2030-10-27"\nend_date="2031-03-29"\n'
            '[vamsys]\ndb_path="X:/old/workspaces.db"\n'
            '[export]\neffective_time="2030-11-01 16:00"\n'
            '[thresholds]\nplan_attach=[30, 40]\n',
            f'[vamsys]\nworkspace_id="{WS}"\n' + CI + '[thresholds]\nplan_attach=[50, 50]\nbank_supersede_days=9\n'
            '[fetch]\ncache_dir="x"\n[season]\nname="ignored"\n')
        cfg = C.load_config(home=home, strict=False)
        self.assertEqual(cfg.effective_time(), "2030-11-01 16:00:00")
        self.assertEqual(cfg.thresholds.plan_attach, (30, 40))                # season beats site
        self.assertEqual(cfg.thresholds.bank_supersede_days, 9)               # site beats default
        self.assertEqual(cfg.thresholds.bank_merge, ((45, 60), (20, 75), (65, 20)))
        self.assertEqual(cfg.workspace_id, WS)
        self.assertEqual(cfg.season_label, "2030-31-1")
        self.assertEqual(cfg.plan_groups, ("国内", "国际", "港澳"))
        self.assertEqual(cfg.fetch.rate_per_sec, 2.0)
        self.assertEqual(cfg.vamsys_db, home.resolve() / "vamsys" / "workspaces.db")   # db_path ignored
        self.assertEqual(cfg.cache_dir, home.resolve() / "cache")
        self.assertEqual(dict(cfg.sources)["thresholds.plan_attach"], "season")
        self.assertEqual(dict(cfg.sources)["vamsys.workspace_id"], "site")
        w = "\n".join(cfg.warnings)
        for needle in ("db_path 已不再使用", "cache_dir 已不再使用", "[season] 只能写在"):
            self.assertIn(needle, w)

    def test_effective_values(self):
        home = self._home('[season]\nname="S"\n[thresholds]\nplan_attach=[30, 40]\n',
                          f'[vamsys]\nworkspace_id="{WS}"\n' + CI + '[thresholds]\nfr24_ac_min_share=0.10\n')
        ev = C.effective_values(home=home)
        rows = {(r["section"], r["key"]): r for r in ev["rows"]}
        self.assertEqual(rows[("thresholds", "plan_attach")]["source"], "season")
        self.assertEqual(rows[("thresholds", "plan_attach")]["value"], [30, 40])
        self.assertTrue(rows[("thresholds", "plan_attach")]["changed"])
        self.assertEqual(rows[("thresholds", "fr24_ac_min_share")]["source"], "site")
        self.assertFalse(rows[("thresholds", "fr24_ac_min_share")]["changed"])      # 0.10 is the default
        self.assertEqual(rows[("vamsys", "workspace_id")]["source_label"], "全站")
        self.assertEqual(rows[("fetch", "rate_per_sec")]["source"], "default")
        self.assertEqual(rows[("season", "name")]["value"], "S")
        self.assertEqual(len(rows), sum(len(v) for v in C.default_values().values()))
        json.dumps(ev, default=str)                                                 # JSON-able for the web page
        with C.text_overrides({home / "settings.toml": f'[vamsys]\nworkspace_id="{WS}"\n[fetch]\nrate_per_sec=1.5\n'}):
            r = {(x["section"], x["key"]): x for x in C.effective_values(home=home)["rows"]}
        self.assertEqual(r[("fetch", "rate_per_sec")]["value"], 1.5)

    def test_season_without_toml_is_discovered(self):
        home = self._home(None, f'[vamsys]\nworkspace_id="{WS}"\n' + CI)
        (home / "seasons" / "empty").mkdir()                    # neither season.toml nor plan files: not a season
        self.assertEqual([p.name for p in C.discover_seasons(home / "seasons")], ["2030-31-1"])
        with self.assertRaises(C.ConfigError):                  # dates cannot be derived from the empty xls files
            C.load_config(home=home, strict=False)
        self.assertEqual(C.resolve_season_dir(None, home / "seasons").name, "2030-31-1")

    def test_missing_home(self):
        with mock.patch.dict(os.environ, {"VCCA_HOME": str(Path(tempfile.gettempdir()) / "vcca-no-such-home")}):
            with self.assertRaises(C.ConfigError) as cm:
                C.vcca_home()
            self.assertIn("数据目录不存在", str(cm.exception))
            with self.assertRaises(C.ConfigError):
                C.load_config()

    def test_missing_season(self):
        with self.assertRaises(C.ConfigError):
            C.load_config("does-not-exist")

    def test_migrate_config(self):
        old = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, old, True)
        (old / "2031-1").mkdir()
        f = old / "2031-1" / "season.toml"
        f.write_text(
            '[season]\nname="冬春"\nstart_date="2031-10-26"\nend_date="2032-03-27"\n'
            'plan_files=["国内航司国内航班计划（三字码）.xls","国内航司国际航班计划（三字码）.xls",'
            '"国内航司港澳台航班计划（三字码）.xls"]\nplan_groups=["国内","国际","港澳"]\nplan_tz_offset_min=480\n'
            f'[vamsys]\ndb_path="../Data/workspaces.db"\nworkspace_id="{WS}"\ncollection=1\n'
            '[airports]\nalias={ ZSJA = "ZSGS" }\nnames={}\n'
            '[export]\neffective_time="next_bjt_midnight"\ndefault_cost_index={ CCA = 80, CAO = 150 }\n'
            '[fetch]\nrate_per_sec=2.0\ncache_dir=""\n'
            '[thresholds]\nfr24_ac_min_share=0.10\nbank_merge=[[45, 60], [20, 75], [65, 20]]\nplan_attach=[50, 60]\n',
            encoding="utf-8")
        m = C.migrate_config(f)
        self.assertEqual(m.season_name, "2031-1")
        self.assertEqual(m.settings, {"vamsys": {"workspace_id": WS}, "airports": {"alias": {"ZSJA": "ZSGS"}},
                                      "export": {"default_cost_index": {"CCA": 80, "CAO": 150}},
                                      "thresholds": {"plan_attach": [50, 60]}})
        self.assertEqual(m.season, {"season": {"name": "冬春", "start_date": "2031-10-26", "end_date": "2032-03-27"}})
        for k in ("vamsys.db_path（不再是配置项）", "fetch.cache_dir（不再是配置项）", "thresholds.fr24_ac_min_share（= 默认）",
                  "season.plan_groups（= 默认）"):
            self.assertIn(k, m.dropped)
        self.assertEqual(tomllib.loads(m.settings_text), m.settings)
        self.assertEqual(tomllib.loads(m.season_text), m.season)

    def test_credentials_never_expose_values(self):
        with mock.patch.dict(os.environ, {"FR24_USER": "u@example.invalid", "FR24_PASSWORD": "pw-secret"}):
            st = C.credential_status(S.cfg())
            self.assertEqual(st["source"], "ENV")
            self.assertEqual(C.fr24_credentials(), ("u@example.invalid", "pw-secret"))
        self.assertNotIn("pw-secret", json.dumps(st))
        for path, hu, hp in st["checked"]:
            self.assertIsInstance(hu, bool)
            self.assertIsInstance(hp, bool)
        with mock.patch.dict(os.environ, {"FR24_USER": "", "FR24_PASSWORD": ""}):
            self.assertIsNone(C.credential_status()["source"])
            self.assertEqual(C.fr24_credentials(), (None, None))

    def test_web_account_file_wins_over_environment(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(C, "fr24_account_path", return_value=Path(td) / C.FR24_ACCOUNT_FILE), \
                mock.patch.dict(os.environ, {"FR24_USER": "env@example.invalid", "FR24_PASSWORD": "env-pw"}):
            self.assertEqual(C.read_fr24_account(), ("", ""))
            p = C.save_fr24_account("web@example.invalid", "web-pw")
            if os.name == "posix":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(C.fr24_credentials(), ("web@example.invalid", "web-pw"))
            st = C.credential_status()
            self.assertEqual((st["source"], [c[1:] for c in st["checked"]]), ("WEB", [(True, True), (True, True)]))
            self.assertNotIn("web-pw", json.dumps(st))
            self.assertTrue(C.clear_fr24_account())
            self.assertFalse(C.clear_fr24_account())
            self.assertEqual(C.fr24_credentials(), ("env@example.invalid", "env-pw"))
            self.assertEqual(C.credential_status()["source"], "ENV")


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
