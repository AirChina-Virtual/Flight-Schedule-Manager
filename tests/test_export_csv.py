"""export_csv: routing, formatting (byte style of the FlightOpsDesk examples), validation."""

import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

from tests import _support as S

sys.path.insert(0, str(S.WORK_DIR / "tests" / "fixtures" / "export"))
import export_world as W  # noqa: E402

from vcca import export_csv as E  # noqa: E402
from vcca.models import CSV_FILE, DecisionCategory  # noqa: E402

EXAMPLES = S.WORK_DIR.parent / "2026-27-1" / "flightopsdesk_csv"


def read_rows(p: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(p.read_bytes().decode("utf-8"), newline="")))


class TestRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.meta, cls.ds, cls.vs = W.world()
        cls.plan = E.plan_csv(cls.meta, cls.ds, S.cfg())

    def ids(self, key):
        return [r["ID"] for r in self.plan.files.get(key, [])]

    def test_counts(self):
        self.assertEqual(self.plan.counts, {
            "01_airports_add": 1, "02_routes_retire": 2, "02b_routes_retire_deferred": 1,
            "03_routes_update": 2, "03_routes_update_待复核": 1,
            "04_routes_create": 4, "04_routes_create_待复核": 1})

    def test_retire(self):
        self.assertEqual(self.ids("02_routes_retire"), ["103", "104"])     # 106 already has an End Date
        self.assertEqual(self.ids("02b_routes_retire_deferred"), ["105"])
        self.assertEqual(self.plan.skipped.get("already_retiring"), 1)
        for r in self.plan.files["02_routes_retire"]:
            self.assertEqual(r["End Date"], W.EFFECTIVE)

    def test_update(self):
        self.assertEqual(self.ids("03_routes_update"), ["100", "101"])    # 110 no changes, 111 end_date only
        self.assertEqual(self.ids("03_routes_update_待复核"), ["109"])
        r100, r101 = self.plan.files["03_routes_update"]
        self.assertEqual((r100["Departure Time (HH:MM)"], r100["Arrival Time (HH:MM)"]), ("01:20", "03:30"))
        self.assertEqual(r100["Cost Index"], "80")
        # empty target fields never blank out VAMSYS values; cost index = current (blank stays blank)
        self.assertEqual(r101["Fleet IDs"], "26072")
        self.assertEqual(r101["Cost Index"], "")
        self.assertEqual(r101["Service Days"], "monday,wednesday,friday,sunday")
        self.assertTrue(any("03" in w and "111" in w for w in self.plan.warnings))

    def test_create(self):
        rows = self.plan.files["04_routes_create"]
        self.assertEqual([r["Callsign"] for r in rows], ["CCA2001", "CCA2002", "CAO2003", "CCA2007"])
        for r in rows:
            self.assertEqual((r["ID"], r["Start Date"], r["Is Hidden"], r["_delete"]), ("", W.EFFECTIVE, "FALSE", "FALSE"))
        self.assertEqual(rows[2]["Cost Index"], "150")                     # CAO default from season config
        self.assertEqual(rows[0]["Fleet IDs"], "26071,26072")

    def test_report_only_never_exported(self):
        exported = {r["Callsign"] for rows in self.plan.files.values() for r in rows if "Callsign" in r}
        for d in self.ds:
            if CSV_FILE.get(d.category) is None and not d.changes:
                self.assertNotIn(d.target.callsign, exported, d.category)
        for cat in DecisionCategory:  # any future category without a CSV mapping is report-only
            if CSV_FILE.get(cat) is None:
                self.assertIsNone(E.stem_for(W._new(cat, "CCA3000", "ZBAA", "ZSSS", "01:00", "02:00", {0})))

    def test_validation_passes(self):
        self.assertEqual(E.validate_plan(self.plan, self.vs), [])


class TestFormat(unittest.TestCase):
    def test_written_files(self):
        meta, ds, vs = W.world()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "03_routes_update_999.csv").write_text("stale", encoding="utf-8")
            (tmp / "keep_me.csv").write_text("x", encoding="utf-8")
            with self.assertLogs("vcca", "WARNING"):
                paths = E.export_csv(tmp, meta, ds, S.cfg(), vs)
            names = sorted(p.name for p in paths)
            self.assertIn("03_routes_update_待复核_1.csv", names)
            self.assertIn("02b_routes_retire_deferred_1.csv", names)
            self.assertFalse((tmp / "03_routes_update_999.csv").exists())
            self.assertTrue((tmp / "keep_me.csv").exists())
            self.assertEqual(E.csv_counts(paths)["04_routes_create"], 4)
            for p in paths:
                raw = p.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), p.name)
                self.assertTrue(raw.endswith(b"FALSE\r\n"), p.name)
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), p.name)
                stem = E.parse_csv_filename(p.name)[0]
                self.assertEqual(raw.split(b"\r\n")[0].decode(), ",".join(E.COLUMNS[stem]))
                self.assertEqual(len(read_rows(p)), E.parse_csv_filename(p.name)[2])
            text = (tmp / "04_routes_create_4.csv").read_bytes().decode("utf-8")
            self.assertIn(',ZBAA,ZBAL,scheduled,CCA2001,CA2001,"26071,26072",2026-11-26 16:00:00,06:00,08:00,'
                          '"monday,thursday",80,FALSE,FALSE\r\n', text)
            self.assertIn(',ZSPD,EBLG,cargo,CAO2003,CA2003,26722,', text)      # single fleet not quoted

    @unittest.skipUnless(EXAMPLES.is_dir(), "FlightOpsDesk example CSVs not present")
    def test_headers_match_examples(self):
        examples = {"01_airports_add": "01_airports_add_*.csv", "02_routes_retire": "02_routes_retire_*.csv",
                    "02b_routes_retire_deferred": "02b_routes_retire_deferred_*.csv",
                    "03_routes_update": "03_routes_update_*.csv", "04_routes_create": "04_routes_create_*.csv"}
        for stem, pat in examples.items():
            files = sorted(EXAMPLES.glob(pat))
            if not files:
                continue
            raw = files[0].read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(raw.split(b"\r\n")[0].decode("utf-8"), ",".join(E.COLUMNS[stem]), stem)
            # our renderer reproduces the example rows byte-for-byte
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))[:50]
            self.assertEqual(E.render_csv(stem, rows).encode("utf-8"),
                             b"\r\n".join(raw.split(b"\r\n")[:len(rows) + 1]) + b"\r\n", stem)


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.meta, self.ds, self.vs = W.world()

    def test_problems_reported(self):
        good = E.plan_csv(self.meta, self.ds, S.cfg()).files["04_routes_create"][0]
        bad = dict(good, **{"Departure Time (HH:MM)": "6:00", "Service Days": "monday,funday",
                            "Fleet IDs": "26071,99999", "Arrival Airport (ICAO/IATA)": "XXXX", "ID": "5"})
        errs = E.validate_rows("04_routes_create", [bad], self.vs, airports_added={"ZBAL"})
        text = "\n".join(errs)
        for frag in ("HH:MM", "funday", "99999", "XXXX", "ID 应为空"):
            self.assertIn(frag, text)
        upd = E.plan_csv(self.meta, self.ds, S.cfg()).files["03_routes_update"][0]
        errs = E.validate_rows("03_routes_update", [dict(upd, ID="424242")], self.vs)
        self.assertTrue(any("424242" in e for e in errs))
        errs = E.validate_rows("02_routes_retire", [{"ID": "103"}], self.vs)
        self.assertTrue(any("列名" in e for e in errs))
        # airport only valid because it is in the 01 file
        self.assertTrue(E.validate_rows("04_routes_create", [good], self.vs))
        self.assertEqual(E.validate_rows("04_routes_create", [good], self.vs, airports_added={"ZBAL"}), [])

    def test_export_fails_loudly(self):
        bad = W.decisions()
        bad[1].target.fleet_ids = ("99999",)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(E.CsvValidationError) as cm:
                E.export_csv(Path(tmp), self.meta, bad, S.cfg(), self.vs)
            self.assertTrue(any("99999" in e for e in cm.exception.errors))
            self.assertEqual(list(Path(tmp).glob("*.csv")), [])


if __name__ == "__main__":
    unittest.main()
