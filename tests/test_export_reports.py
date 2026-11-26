"""export_xlsx sheets / row counts, export_html generation + diff_runs, report_summary."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from tests import _support as S

sys.path.insert(0, str(S.WORK_DIR / "tests" / "fixtures" / "export"))
import export_world as W  # noqa: E402

from vcca import export_html as H, export_xlsx as X, report_summary as RS  # noqa: E402
from vcca.models import dump_decisions  # noqa: E402


class TestXlsx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        meta, ds, vs = W.world()
        cls.path = X.export_xlsx(Path(cls.tmp.name), meta, ds, S.cfg(), vs)
        cls.wb = load_workbook(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.wb.close()
        cls.tmp.cleanup()

    def data_rows(self, name):
        return [r for r in self.wb[name].iter_rows(min_row=2, values_only=True) if any(v is not None for v in r)]

    def test_sheets(self):
        self.assertEqual(tuple(self.wb.sheetnames), X.SHEETS)

    def test_row_counts(self):
        expect = {"1 新增": 5, "2 调整": 5, "3 停用": 4, "4 计划外临时航班": 2, "5 待观察": 2, "6 一致": 2,
                  "7 去回程缺失段": 2, "9 人工复核": 2}
        for name, n in expect.items():
            self.assertEqual(len(self.data_rows(name)), n, name)
            ws = self.wb[name]
            self.assertEqual(ws.freeze_panes, "A2", name)
            self.assertTrue(ws.auto_filter.ref, name)

    def test_update_sheet_fields(self):
        ws = self.wb["2 调整"]
        head = [c.value for c in ws[1]]
        row = next(r for r in self.data_rows("2 调整") if r[0] == "100")
        get = dict(zip(head, row))
        self.assertEqual((get["现行起飞"], get["推荐起飞"]), ("01:00", "01:20"))
        self.assertIn("按FR24实飞校准时刻", get["原因"])
        self.assertEqual(get["CSV"], "03_routes_update")
        row = dict(zip(head, next(r for r in self.data_rows("2 调整") if r[0] == "109")))
        self.assertIn("一三五→一三", row["原因"])        # JSON day lists rendered as weekdays

    def test_review_and_overview(self):
        head = [c.value for c in self.wb["9 人工复核"][1]]
        flags = {dict(zip(head, r))["复核标记"] for r in self.data_rows("9 人工复核")}
        self.assertEqual(flags, {"WEEKDAY_DROPPED", "UNRATED_AIRPORT"})
        text = "\n".join(str(v) for r in self.wb["0 总览"].iter_rows(values_only=True) for v in r if v is not None)
        for frag in ("revision 7", "通过", "time_override_min_acts", "extra_only_hit", "04_routes_create_4.csv",
                     "AIRPORT_ADD"):
            self.assertIn(frag, text)
        audit = "\n".join(str(v) for r in self.wb["8 机队与机场审计"].iter_rows(values_only=True) for v in r if v)
        self.assertIn("ZBAL", audit)
        self.assertIn("BAYANHOT", audit)


class TestDiff(unittest.TestCase):
    def test_diff_runs(self):
        _, cur, _ = W.world()
        _, prev = W.previous_world()
        dr = H.diff_runs(prev, cur)
        self.assertEqual([e["callsign"] for e in dr["added"]], ["CCA2002"])
        self.assertEqual([e["callsign"] for e in dr["removed"]], ["CCA2999"])
        self.assertEqual(len(dr["changed"]), 1)
        ch = dr["changed"][0]
        self.assertEqual((ch["vroute_id"], ch["prev_category"], ch["category"]), ("100", "KEEP", "UPDATE"))
        self.assertEqual({x["field"] for x in ch["diffs"]}, {"category", "dep", "arr", "changes"})
        self.assertEqual(H.diff_runs(cur, cur), {"added": [], "removed": [], "changed": []})

    def test_duplicate_keys_kept(self):
        _, cur, _ = W.world()
        dup = cur + [cur[-2]]
        self.assertEqual(len(H.diff_runs(cur, dup)["added"]), 1)


class TestHtml(unittest.TestCase):
    def test_generate(self):
        meta, ds, vs = W.world()
        pmeta, prev = W.previous_world()
        with tempfile.TemporaryDirectory() as tmp:
            prev_dir = Path(tmp) / "2026-11-26_0100"
            dump_decisions(prev_dir / "decisions.json", pmeta, prev)
            ds[0].details["name"] = "</script><b>x</b>"   # must stay inert inside the JSON blob
            p = H.export_html(Path(tmp) / "run", meta, ds, S.cfg(), previous_run_dir=prev_dir, vamsys=vs)
            html = p.read_text(encoding="utf-8")
        self.assertEqual(html.count("</script>"), 2)
        self.assertNotRegex(html, r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""")   # no external resources
        self.assertIn("prefers-color-scheme:dark", html)
        blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
        data = json.loads(blob)
        self.assertEqual(len(data["rows"]), len(ds))
        self.assertEqual(data["diff"]["prev_run"], "2026-11-26_0100")
        self.assertEqual(len(data["diff"]["added"]), 1)
        self.assertEqual(data["rows"][0]["det"]["name"], "</script><b>x</b>")
        self.assertTrue(any(c["code"] == "UPDATE" and c["n"] == 5 for c in data["cats"]))
        self.assertEqual(data["cal_range"]["start"], meta.season_start)
        self.assertEqual(data["cal_range"]["tol"], S.cfg().thresholds.plan_attach[0])
        self.assertIn("cal", data["rows"][0]["ev"])
        self.assertIn("function calendar(r)", html)
        self.assertIn("function slotTable(r)", html)

    def test_without_previous(self):
        meta, ds, _ = W.world()
        with tempfile.TemporaryDirectory() as tmp:
            html = H.export_html(Path(tmp), meta, ds, S.cfg()).read_text(encoding="utf-8")
        self.assertIn('"diff":null', html)


class TestSummary(unittest.TestCase):
    def test_summary(self):
        meta, ds, vs = W.world()
        s = RS.summarize(meta, ds, csv_counts={"02_routes_retire": 2}, vamsys=vs, warnings=["w1"])
        self.assertTrue(s["closure_ok"])
        self.assertEqual(s["needs_review"], 2)
        self.assertEqual(s["counts"]["UPDATE"], 5)
        self.assertEqual(s["csv"], {"02_routes_retire": 2})
        self.assertIn("w1", s["warnings"])
        with tempfile.TemporaryDirectory() as tmp:
            p = RS.write_summary(Path(tmp), meta, ds, csv_counts={"x": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["csv"], {"x": 1})

    def test_closure_detects_problems(self):
        meta, ds, vs = W.world()
        ds = [d for d in ds if d.vroute_id != "102"] + [d for d in ds if d.vroute_id == "100"]
        c = RS.closure_check(meta, ds, vs)
        self.assertFalse(c["ok"])
        self.assertEqual(c["duplicates"], ["100"])
        self.assertEqual(c["undecided_now"], ["102"])

    def test_fmt_ts(self):
        self.assertEqual(RS.fmt_ts(1795662300), "2026-11-26 11:05 BJT (11-26 03:05Z)")
        self.assertEqual(RS.fmt_ts(None), "")


if __name__ == "__main__":
    import unittest as _u

    _u.main()
