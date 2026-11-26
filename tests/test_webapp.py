"""Web app (vcca/webapp.py): auth, CSRF, path safety, read APIs, config editing, uploads, portability checks.

The WSGI application is called directly (no port). Everything runs in temp dirs: VCCA_PLAN_ROOT / VCCA_STATE_DIR /
VCCA_RULES_DIR (a copy of rules/) / VCCA_CACHE_DIR; the real season, rules, VAMSYS DB and cache are never written.
"""

import ast
import dataclasses
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import _support as S

sys.path.insert(0, str(S.WORK_DIR / "tests" / "fixtures" / "export"))
import export_world as W  # noqa: E402

from vcca import config as C, export_html as H, webapp  # noqa: E402
from vcca.models import dump_decisions  # noqa: E402

SEASON = "2099-1"
RUN, PREV = "2099-01-02_0300", "2099-01-01_0300"
WS_ID = "b8d41af1-5641-4b06-a476-b39829a63d25"
CSV_NAME = "03_routes_update_待复核_2.csv"
REAL_PLAN_XLS = S.WORK_DIR.parent / S.SEASON / "国内航司港澳台航班计划（三字码）.xls"


def make_vamsys_db(path: Path, ws: str = WS_ID, revision: int = 7) -> Path:
    """Minimal workspaces.db that vamsys.load_vamsys can read (no rows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE workspaces(id TEXT PRIMARY KEY, name TEXT, json TEXT);
        CREATE TABLE workspace_revisions(workspace_id TEXT, revision INTEGER);
        CREATE TABLE resource_rows(workspace_id TEXT, resource INTEGER, collection INTEGER, ordinal INTEGER, json TEXT);
    """)
    conn.execute("INSERT INTO workspaces VALUES (?,?,?)", (ws, "Test WS", json.dumps({"StorageRevision": revision})))
    conn.execute("INSERT INTO workspace_revisions VALUES (?,?)", (ws, revision))
    conn.commit()
    conn.close()
    return path


SEASON_TOML = f"""# test season
[season]
name = "测试航季"
start_date = "2026-10-25"
end_date = "2027-03-27"
plan_files = ["plan_a.xls"]

[vamsys]
db_path = "Data/workspaces.db"
workspace_id = "{WS_ID}"

[export]
default_cost_index = {{ CCA = 80, CAO = 150, CCD = 80, BJN = 80 }}
"""


class WebTestCase(unittest.TestCase):
    """Fresh temp world per test class; ``self.call`` runs one request through the WSGI app."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.root, cls.state = base / "plan", base / "state"
        cls.rules, cls.cache = base / "rules", base / "cache"
        shutil.copytree(C.WORK_DIR / "rules", cls.rules, ignore=shutil.ignore_patterns(".history"))
        cls.cache.mkdir()
        cls.sdir = cls.root / SEASON
        (cls.sdir / "output").mkdir(parents=True)
        (cls.sdir / "season.toml").write_bytes(SEASON_TOML.encode("utf-8"))
        make_vamsys_db(cls.root / "Data" / "workspaces.db")
        meta, ds, _ = W.world()
        for run, (m, d) in ((RUN, (meta, ds)), (PREV, W.previous_world())):
            rd = cls.sdir / "output" / run
            rd.mkdir()
            dump_decisions(rd / "decisions.json", m, d)
        (cls.sdir / "output" / RUN / CSV_NAME).write_bytes(b"a,b\r\n1,2\r\n")
        (cls.sdir / "output" / RUN / "summary.json").write_text(json.dumps({"counts": {"UPDATE": 1}}),
                                                                 encoding="utf-8")
        cls.env = mock.patch.dict(os.environ, {"VCCA_PLAN_ROOT": str(cls.root), "VCCA_STATE_DIR": str(cls.state),
                                               "VCCA_RULES_DIR": str(cls.rules), "VCCA_CACHE_DIR": str(cls.cache)})
        cls.env.start()
        webapp.set_user(cls.state, "alice", "correct horse", iterations=1000)
        cls.app = webapp.App(cls.state)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, *, query="", cookie=None, csrf=None, headers=None, raw=None, ip="10.0.0.1"):
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else b"")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path.encode("utf-8").decode("latin-1"), "QUERY_STRING": query,
               "wsgi.input": io.BytesIO(data), "CONTENT_LENGTH": str(len(data)), "REMOTE_ADDR": ip,
               "wsgi.url_scheme": "http", "HTTP_COOKIE": cookie or ""}
        if csrf:
            env["HTTP_X_VCCA_CSRF"] = csrf
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}

        def start_response(status, hdrs):
            out["status"], out["headers"] = int(status.split()[0]), hdrs
        body_bytes = b"".join(self.app(env, start_response))
        hd = {}
        for k, v in out["headers"]:
            hd.setdefault(k.lower(), []).append(v)
        if "gzip" in hd.get("content-encoding", []):
            body_bytes = gzip.decompress(body_bytes)
        return out["status"], hd, body_bytes

    def jcall(self, *a, **kw):
        st, hd, b = self.call(*a, **kw)
        return st, (json.loads(b) if b and hd["content-type"][0].startswith("application/json") else None)

    def login(self, user="alice", pw="correct horse", ip="10.0.0.1"):
        st, hd, b = self.call("POST", "/api/login", {"username": user, "password": pw}, ip=ip)
        self.assertEqual(st, 200, b)
        cookie = hd["set-cookie"][0].split(";")[0]
        return cookie, json.loads(b)["csrf"]

    def authed(self, method, path, body=None, **kw):
        if not hasattr(self, "_auth"):
            self._auth = self.login()
        cookie, csrf = self._auth
        return self.jcall(method, path, body, cookie=cookie, csrf=csrf, **kw)


class TestAuth(WebTestCase):
    def test_unauthenticated_is_401_but_shell_is_public(self):
        for p in ("/api/status", "/api/runs", "/api/me", f"/files/{SEASON}/{RUN}/{CSV_NAME}", "/api/jobs"):
            self.assertEqual(self.call("GET", p)[0], 401, p)
        st, hd, b = self.call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"static/app.js", b)
        self.assertIn("default-src 'self'", hd["content-security-policy"][0])
        self.assertEqual(hd["x-content-type-options"], ["nosniff"])
        self.assertEqual(self.call("GET", "/static/report.js")[0], 200)

    def test_login_logout_and_cookie_flags(self):
        st, _ = self.jcall("POST", "/api/login", {"username": "alice", "password": "nope"}, ip="10.9.9.9")
        self.assertEqual(st, 401)
        st, hd, b = self.call("POST", "/api/login", {"username": "alice", "password": "correct horse"})
        self.assertEqual(st, 200)
        ck = hd["set-cookie"][0]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, ck)
        self.assertNotIn("Secure", ck)
        cookie, csrf = ck.split(";")[0], json.loads(b)["csrf"]
        st, me = self.jcall("GET", "/api/me", cookie=cookie)
        self.assertEqual((st, me["user"], me["csrf"]), (200, "alice", csrf))
        self.assertEqual(self.jcall("POST", "/api/logout", {}, cookie=cookie, csrf=csrf)[0], 200)
        self.assertEqual(self.jcall("GET", "/api/me", cookie=cookie)[0], 401)

    def test_secure_cookie_behind_https_proxy(self):
        with mock.patch.dict(os.environ, {"VCCA_TRUST_PROXY": "1"}):
            st, hd, _ = self.call("POST", "/api/login", {"username": "alice", "password": "correct horse"},
                                  headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(st, 200)
        self.assertIn("Secure", hd["set-cookie"][0])

    def test_rate_limit(self):
        for _ in range(3):
            st, _ = self.jcall("POST", "/api/login", {"username": "alice", "password": "bad"}, ip="10.7.7.7")
            self.assertEqual(st, 401)
        st, d = self.jcall("POST", "/api/login", {"username": "alice", "password": "correct horse"}, ip="10.7.7.7")
        self.assertEqual(st, 429)
        self.assertGreater(d["retry_after"], 0)
        self.assertEqual(self.jcall("POST", "/api/login", {"username": "alice", "password": "correct horse"},
                                    ip="10.7.7.8")[0], 200)

    def test_csrf_required_for_writes(self):
        cookie, csrf = self.login()
        self.assertEqual(self.jcall("POST", "/api/jobs", {"cmd": "status"}, cookie=cookie)[0], 403)
        self.assertEqual(self.jcall("POST", "/api/jobs", {"cmd": "status"}, cookie=cookie, csrf="wrong")[0], 403)
        self.assertEqual(self.jcall("POST", "/api/config/validate", {"key": "season", "text": ""},
                                    cookie=cookie)[0], 403)

    def test_password_hashing(self):
        h = webapp.hash_password("pw", iterations=1000)
        self.assertTrue(h.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(webapp.verify_password("pw", h))
        self.assertFalse(webapp.verify_password("pw2", h))
        self.assertFalse(webapp.verify_password("pw", "garbage"))
        text = (self.state / webapp.USERS_FILE).read_text(encoding="utf-8")
        self.assertNotIn("correct horse", text)
        self.assertIn('[users."alice"]', text)
        with self.assertRaises(ValueError):
            webapp.set_user(self.state, "bad name!", "x")

    def test_session_survives_restart(self):
        cookie, _ = self.login()
        app2 = webapp.App(self.state, job_manager=self.app.jobs)
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/me", "QUERY_STRING": "", "wsgi.input": io.BytesIO(b""),
               "HTTP_COOKIE": cookie, "REMOTE_ADDR": "x", "wsgi.url_scheme": "http"}
        res = {}
        b"".join(app2(env, lambda s, h: res.setdefault("s", s)))
        self.assertTrue(res["s"].startswith("200"))


class TestReadApis(WebTestCase):
    def test_seasons_and_status(self):
        st, d = self.authed("GET", "/api/seasons")
        self.assertEqual((st, [s["name"] for s in d["seasons"]], d["default"]), (200, [SEASON], SEASON))
        st, d = self.authed("GET", "/api/status", query=f"season={SEASON}")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertEqual(d["config"]["season_label"], "测试航季")
        self.assertFalse(d["plan"]["ok"])                       # plan_a.xls does not exist -> warning, not error
        self.assertTrue(any("plan_a.xls" in w for w in d["config"]["warnings"]))
        self.assertTrue(d["vamsys"]["ok"], d["vamsys"])
        self.assertEqual(d["vamsys"]["revision"], 7)
        self.assertEqual(d["cache"]["dir"], str(self.cache))
        self.assertEqual(d["runs"]["count"], 2)
        self.assertEqual(self.authed("GET", "/api/status", query="season=nope")[0], 404)
        self.assertEqual(self.authed("GET", "/api/status", query="season=..%2F..")[0], 400)

    def test_runs_and_run(self):
        st, d = self.authed("GET", "/api/runs", query=f"season={SEASON}")
        self.assertEqual([r["name"] for r in d["runs"]], [RUN, PREV])
        self.assertEqual(d["runs"][0]["csv"], [CSV_NAME])
        self.assertEqual(d["runs"][0]["counts"], {"UPDATE": 1})
        st, d = self.authed("GET", f"/api/runs/{RUN}", query=f"season={SEASON}")
        self.assertEqual(st, 200)
        self.assertEqual(d["files"][0]["name"], CSV_NAME)
        self.assertEqual(d["earlier_runs"], [PREV])

    def test_report_payload(self):
        st, d = self.authed("GET", f"/api/runs/{RUN}/report", query=f"season={SEASON}")
        self.assertEqual(st, 200)
        meta, ds, _ = W.world()
        self.assertEqual(len(d["rows"]), len(ds))
        self.assertEqual(d["diff"]["prev_run"], PREV)
        self.assertEqual([e["callsign"] for e in d["diff"]["added"]], ["CCA2002"])
        st, d2 = self.authed("GET", f"/api/runs/{RUN}/report", query=f"season={SEASON}&prev=none")
        self.assertIsNone(d2["diff"])
        st, d3 = self.authed("GET", f"/api/runs/{PREV}/report", query=f"season={SEASON}&prev={RUN}")
        self.assertEqual(d3["diff"]["prev_run"], RUN)
        self.assertEqual(self.authed("GET", f"/api/runs/{RUN}/report", query=f"season={SEASON}&prev=x")[0], 404)

    def test_download_and_zip(self):
        cookie, _ = self.login()
        st, hd, b = self.call("GET", f"/files/{SEASON}/{RUN}/{CSV_NAME}", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual(b, b"a,b\r\n1,2\r\n")
        self.assertIn("filename*=UTF-8''03_routes_update_%E5%BE%85%E5%A4%8D%E6%A0%B8_2.csv",
                      hd["content-disposition"][0])
        st, hd, b = self.call("GET", f"/api/runs/{RUN}/zip", query=f"season={SEASON}", cookie=cookie)
        self.assertEqual(st, 200)
        names = zipfile.ZipFile(io.BytesIO(b)).namelist()
        self.assertIn(f"{SEASON}_{RUN}/{CSV_NAME}", names)
        self.assertIn(f"{SEASON}_{RUN}/decisions.json", names)
        self.assertEqual(list((self.state / "tmp").iterdir()), [])          # temp zip removed after streaming

    def test_path_traversal_rejected(self):
        cookie, _ = self.login()
        (self.root / "secret.txt").write_text("x", encoding="utf-8")
        bad = [f"/files/{SEASON}/{RUN}/..", f"/files/{SEASON}/{RUN}/..\\..\\secret.txt",
               f"/files/{SEASON}/{RUN}/%2e%2e%2fsecret.txt", f"/files/{SEASON}/..%2F/season.toml",
               f"/files/../{RUN}/decisions.json", f"/files/{SEASON}/{RUN}/.hidden",
               f"/files/{SEASON}/output/season.toml", "/static/..%2Fwebapp.py", "/static/../webapp.py",
               f"/api/runs/..%2F..%2Fsecret.txt", f"/files/{SEASON}/{RUN}/nope.csv"]
        for p in bad:
            st = self.call("GET", p, cookie=cookie)[0]
            self.assertIn(st, (400, 404), p)
        st = self.call("GET", f"/files/{SEASON}/{RUN}/../../season.toml", cookie=cookie)[0]
        self.assertIn(st, (400, 404))

    def test_unknown_route_and_method(self):
        self.assertEqual(self.authed("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.authed("DELETE", "/api/jobs")[0], 405)


class TestConfigEditing(WebTestCase):
    def setUp(self):
        self.path = self.sdir / "season.toml"
        self.path.write_bytes(SEASON_TOML.encode("utf-8"))
        shutil.rmtree(self.sdir / ".config_history", ignore_errors=True)

    def get_file(self, key="season"):
        st, d = self.authed("GET", "/api/config", query=f"season={SEASON}")
        return next(f for f in d["files"] if f["key"] == key)

    def test_validate_syntax_and_config_errors(self):
        st, d = self.authed("POST", "/api/config/validate", {"season": SEASON, "key": "season",
                                                              "text": SEASON_TOML + "\nbroken = = 1\n"})
        self.assertEqual(st, 400)
        self.assertEqual(d["stage"], "syntax")
        self.assertEqual(d["syntax"]["line"], SEASON_TOML.count("\n") + 2)
        bad = SEASON_TOML.replace('end_date = "2027-03-27"', 'end_date = "2020-01-01"')
        st, d = self.authed("POST", "/api/config/validate", {"season": SEASON, "key": "season", "text": bad})
        self.assertEqual((st, d["stage"]), (422, "config"))
        self.assertTrue(any("必须早于" in e for e in d["errors"]))
        st, d = self.authed("POST", "/api/config/validate", {"season": SEASON, "key": "season", "text": SEASON_TOML})
        self.assertEqual(st, 200)
        self.assertEqual(d["summary"]["start"], "2026-10-25")
        self.assertEqual(self.path.read_text(encoding="utf-8"), SEASON_TOML)          # never written

    def test_save_conflict_failure_success_and_restore(self):
        f = self.get_file()
        st, d = self.authed("POST", "/api/config/save", {"season": SEASON, "key": "season", "text": SEASON_TOML,
                                                          "mtime_ns": 123})
        self.assertEqual((st, d["stage"]), (409, "conflict"))
        bad = SEASON_TOML.replace('workspace_id = "', 'workspace_id = "x')
        st, d = self.authed("POST", "/api/config/save", {"season": SEASON, "key": "season", "text": bad,
                                                          "mtime_ns": f["mtime_ns"]})
        self.assertEqual(st, 422)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SEASON_TOML)          # unchanged on failure
        new = SEASON_TOML.replace('name = "测试航季"', 'name = "测试航季2"')
        st, d = self.authed("POST", "/api/config/save", {"season": SEASON, "key": "season", "text": new,
                                                          "mtime_ns": f["mtime_ns"]})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["saved"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), new)
        hist = list((self.sdir / ".config_history").glob("season.*.toml"))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].read_text(encoding="utf-8"), SEASON_TOML)
        f2 = self.get_file()
        self.assertEqual(len(f2["history"]), 1)
        ver = f2["history"][0]["version"]
        st, d = self.authed("GET", "/api/config/history", query=f"season={SEASON}&key=season&version={ver}")
        self.assertEqual(d["text"], SEASON_TOML)
        st, d = self.authed("POST", "/api/config/restore", {"season": SEASON, "key": "season", "version": ver,
                                                             "mtime_ns": f2["mtime_ns"]})
        self.assertEqual(st, 200, d)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SEASON_TOML)
        self.assertEqual(self.authed("GET", "/api/config/history",
                                     query=f"season={SEASON}&key=season&version=../../x")[0], 400)

    def test_rules_file_uses_rules_dir_override(self):
        f = self.get_file("fleets")
        self.assertEqual(Path(f["path"]), self.rules / "fleets.toml")
        st, d = self.authed("POST", "/api/config/validate", {"season": SEASON, "key": "fleets",
                                                              "text": "[cca_narrowbody]\n"})
        self.assertEqual(st, 422)
        self.assertTrue(any("cca_narrowbody" in e for e in d["errors"]))

    def test_save_refused_while_job_running(self):
        f = self.get_file()
        with mock.patch.object(self.app.jobs, "current", return_value={"id": "x", "cmd": "fetch"}):
            st, d = self.authed("POST", "/api/config/save", {"season": SEASON, "key": "season",
                                                              "text": SEASON_TOML, "mtime_ns": f["mtime_ns"]})
        self.assertEqual(st, 409)


class TestUploads(WebTestCase):
    def upload(self, path, data, *, query, filename=None):
        hdr = {"X-Filename": filename} if filename else None
        return self.authed("POST", path, raw=data, query=query, headers=hdr)

    def test_vamsys_upload(self):
        target = self.root / "Data" / "workspaces.db"
        st, d = self.upload("/api/upload/vamsys", b"not sqlite at all, sorry", query=f"season={SEASON}")
        self.assertEqual(st, 400)
        self.assertIn("SQLite", d["error"])
        other = make_vamsys_db(Path(self.tmp.name) / "other.db", ws="00000000-0000-0000-0000-000000000000")
        st, d = self.upload("/api/upload/vamsys", other.read_bytes(), query=f"season={SEASON}")
        self.assertEqual(st, 400)
        self.assertIn("工作区", d["error"])
        new = make_vamsys_db(Path(self.tmp.name) / "new.db", revision=99)
        st, d = self.upload("/api/upload/vamsys", new.read_bytes(), query=f"season={SEASON}")
        self.assertEqual(st, 200, d)
        self.assertEqual((d["revision"], d["workspace_name"]), (99, "Test WS"))
        self.assertTrue((target.parent / d["backup"]).is_file())
        self.assertEqual(target.read_bytes(), new.read_bytes())
        self.assertEqual([p.name for p in target.parent.glob(".upload-*")], [])
        st, dd = self.authed("GET", "/api/data", query=f"season={SEASON}")
        self.assertEqual(dd["vamsys"]["revision"], 99)
        self.assertEqual(len(dd["vamsys"]["backups"]), 1)

    def test_upload_limits(self):
        with mock.patch.dict(os.environ, {"VCCA_MAX_UPLOAD_MB": "0.00001"}):
            st, d = self.upload("/api/upload/vamsys", webapp.SQLITE_MAGIC + b"x" * 100, query=f"season={SEASON}")
        self.assertEqual(st, 413)

    def test_plan_upload_rejects(self):
        q = f"season={SEASON}"
        st, d = self.upload("/api/upload/plan", b"\x00" * 64, query=q, filename="plan_b.xls")
        self.assertEqual(st, 400)
        self.assertIn(".xls", d["error"])
        st, d = self.upload("/api/upload/plan", webapp.OLE_MAGIC + b"\x00" * 64, query=q, filename="../evil.xls")
        self.assertEqual(st, 400)                                   # OLE header but not a parsable workbook
        self.assertFalse((self.root / "evil.xls").exists())
        st, d = self.upload("/api/upload/plan", webapp.OLE_MAGIC, query=q, filename="x.exe")
        self.assertEqual(st, 400)
        self.assertEqual([p.name for p in self.sdir.glob(".upload-*")], [])

    @unittest.skipUnless(REAL_PLAN_XLS.is_file(), "real CAAC plan file not available")
    def test_plan_upload_success_and_plan_files(self):
        data = REAL_PLAN_XLS.read_bytes()
        st, d = self.upload("/api/upload/plan", data, query=f"season={SEASON}&add=1", filename="plan_b（三字码）.xls")
        self.assertEqual(st, 200, d)
        self.assertGreater(d["rows"], 0)
        self.assertTrue(d["config"]["ok"], d["config"])
        self.assertTrue((self.sdir / "plan_b（三字码）.xls").is_file())
        import tomllib
        files = tomllib.loads((self.sdir / "season.toml").read_text(encoding="utf-8"))["season"]["plan_files"]
        self.assertEqual(files, ["plan_a.xls", "plan_b（三字码）.xls"])
        st, d = self.upload("/api/upload/plan", data, query=f"season={SEASON}", filename="plan_b（三字码）.xls")
        self.assertEqual(st, 200)
        self.assertTrue((self.sdir / ".plan_history" / d["backup"]).is_file())

    def test_set_season_array_crlf(self):
        text = '[season]  # s\r\nname = "x"\r\nplan_files = ["a.xls"]\r\n\r\n[vamsys]\r\n'
        out = webapp.App._set_season_array(text, "plan_files", ["a.xls", "b.xls"], multiline=True)
        import tomllib
        self.assertEqual(tomllib.loads(out)["season"]["plan_files"], ["a.xls", "b.xls"])
        self.assertNotIn("\n", out.replace("\r\n", ""))

    def test_set_season_array_keeps_comments_and_other_tables(self):
        text = '# top\n[season]\nname = "x"\nplan_files = [\n  "a.xls",  # c\n]\nplan_groups = ["A"]\n\n[vamsys]\nplan_files = 1\n'
        out = webapp.App._set_season_array(text, "plan_groups", ["A", "B"])
        self.assertIn('plan_groups = ["A", "B"]', out)
        self.assertIn("[vamsys]\nplan_files = 1", out)
        out2 = webapp.App._set_season_array(text, "plan_files", ["a.xls", "b.xls"], multiline=True)
        import tomllib
        self.assertEqual(tomllib.loads(out2)["season"]["plan_files"], ["a.xls", "b.xls"])
        self.assertEqual(tomllib.loads(out2)["vamsys"]["plan_files"], 1)
        self.assertIsNone(webapp.App._set_season_array("[vamsys]\n", "plan_files", []))

    def test_new_season(self):
        st, d = self.authed("POST", "/api/seasons", {"name": "2100-1", "copy_from": SEASON})
        self.assertEqual(st, 200, d)
        self.assertIn("测试航季", (self.root / "2100-1" / "season.toml").read_text(encoding="utf-8"))
        self.assertEqual(self.authed("POST", "/api/seasons", {"name": "2100-1", "copy_from": SEASON})[0], 409)
        self.assertEqual(self.authed("POST", "/api/seasons", {"name": "../x", "copy_from": SEASON})[0], 400)
        shutil.rmtree(self.root / "2100-1")

    def test_first_season_from_template(self):
        import tomllib
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"VCCA_PLAN_ROOT": t}):
            self.assertEqual(self.authed("GET", "/api/data")[0], 404)            # no season yet
            st, d = self.authed("POST", "/api/seasons", {"name": "2027-1", "copy_from": ""})
            self.assertEqual(st, 200, d)
            data = tomllib.loads((Path(t) / "2027-1" / "season.toml").read_text(encoding="utf-8"))
            self.assertEqual(data["season"]["name"], "2027-1")
            self.assertEqual(data["vamsys"]["db_path"], "Data/workspaces.db")
            st, d = self.authed("GET", "/api/status", query="season=2027-1")
            self.assertFalse(d["ok"])                                             # workspace_id still to fill in
            self.assertTrue(any("workspace_id" in e for e in d["errors"]))


class TestPortability(unittest.TestCase):
    PKG = S.WORK_DIR / "vcca"

    def test_env_overrides(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"VCCA_PLAN_ROOT": t, "VCCA_STATE_DIR": t + "/st",
                                              "VCCA_RULES_DIR": t + "/r", "VCCA_CACHE_DIR": t + "/c"}):
                self.assertEqual(C.plan_root(), Path(t).resolve())
                self.assertEqual(C.state_dir(), Path(t, "st").resolve())
                self.assertEqual(C.rules_dir(), Path(t, "r").resolve())
                self.assertEqual(C.default_cache_dir(), Path(t, "c").resolve())
                self.assertEqual(C.discover_seasons(), [])
                (Path(t) / "s1").mkdir()
                (Path(t) / "s1" / "season.toml").write_text("", encoding="utf-8")
                self.assertEqual([p.name for p in C.discover_seasons()], ["s1"])
        self.assertEqual(C.plan_root(), C.WORK_DIR.parent)
        self.assertEqual(C.state_dir(), C.WORK_DIR / "webstate")

    def _modules(self):
        return sorted(self.PKG.glob("*.py")) + [S.WORK_DIR / "vcca_plan.py"]

    def test_no_startfile_or_drive_letters(self):
        for p in self._modules():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                    and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and n.attr == "startfile":
                    self.fail(f"{p.name}:{n.lineno} uses os.startfile")
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    self.assertIsNone(re.match(r"[A-Za-z]:[\\/]", n.value), f"{p.name}:{n.lineno} {n.value!r}")

    def test_text_io_has_explicit_encoding(self):
        for p in self._modules():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                kws = {k.arg for k in n.keywords}
                name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                if name in ("read_text", "write_text"):
                    self.assertIn("encoding", kws, f"{p.name}:{n.lineno} {name} without encoding")
                if name == "open" and isinstance(n.func, ast.Name):
                    mode = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) else \
                        next((k.value.value for k in n.keywords if k.arg == "mode"), "r")
                    if "b" not in str(mode):
                        self.assertIn("encoding", kws, f"{p.name}:{n.lineno} open() text mode without encoding")


class TestRendererShared(unittest.TestCase):
    def test_report_html_inlines_shared_assets(self):
        css, js = H.report_assets()
        self.assertIn("VccaReport", js)
        meta, ds, vs = W.world()
        html = H.render_html(H.build_payload(meta, ds, S.cfg(), vamsys=vs))
        self.assertIn(js, html)
        self.assertIn(css, html)
        self.assertIn("VccaReport.mount(", html)
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("__JS__", html)

    def test_payload_for_run(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "s" / "output"
            meta, ds, vs = W.world()
            for run, (m, d) in ((RUN, (meta, ds)), (PREV, W.previous_world())):
                (out / run).mkdir(parents=True)
                dump_decisions(out / run / "decisions.json", m, d)
            cfg = dataclasses.replace(S.cfg(), output_root=out)
            p_auto = H.payload_for_run(cfg, out / RUN, vamsys=vs)
            self.assertEqual(p_auto["diff"]["prev_run"], PREV)
            direct = H.build_payload(meta, ds, S.cfg(), vamsys=vs, previous=(PREV, W.previous_world()[1]))
            self.assertEqual(json.dumps(p_auto, sort_keys=True, default=str),
                             json.dumps(direct, sort_keys=True, default=str))
            self.assertIsNone(H.payload_for_run(cfg, out / RUN, prev=None, vamsys=vs)["diff"])
            self.assertIsNone(H.payload_for_run(cfg, out / PREV, vamsys=vs)["diff"])
            self.assertTrue(all("key" in c for c in p_auto["csv"]))


if __name__ == "__main__":
    unittest.main()
