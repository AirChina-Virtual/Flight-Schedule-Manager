"""Job manager (vcca/jobs.py): argv whitelist, run -> log -> done, progress / run-dir parsing, one job at a time,
cancel (real process + mocked killpg), restart recovery. Jobs run a tiny fake script, not vcca_plan.py."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import _support as S  # noqa: F401 - sets sys.path

from vcca import jobs as J

FAKE = textwrap.dedent(r'''
    import signal, sys, time
    args = sys.argv[1:]
    cmd = [a for a in args if not a.startswith("-") and a not in ("S1",)][0]
    try:
        if cmd == "status":
            print("hello 状态", flush=True)
            print("  [3/10] CA1234     已用 0:01  预计剩余 0:05", flush=True)
            print("decisions.json -> /tmp/out/2099-01-01_0000", flush=True)
            print("args " + " ".join(args), flush=True)
        elif cmd in ("build", "fetch"):          # long-running
            for i in range(600):
                print(f"tick {i}", flush=True)
                time.sleep(0.05)
        elif cmd == "test":
            sys.exit(3)
    except KeyboardInterrupt:
        print("已中断。", flush=True)
        sys.exit(130)
''')


class JobTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "fake_plan.py"
        self.script.write_text(FAKE, encoding="utf-8")
        self.jm = J.JobManager(self.dir / "state", script=self.script, grace_s=5, kill_s=3)

    def tearDown(self):
        cur = self.jm.current()
        if cur:
            self.jm.cancel(cur["id"])
            self.jm.wait(cur["id"], 20)
        self.tmp.cleanup()


class TestArgv(unittest.TestCase):
    def test_whitelist(self):
        self.assertEqual(J.build_argv("status", "2026-27-1", {}), ["--season", "2026-27-1", "status"])
        self.assertEqual(J.build_argv("fetch", None, {"dry_run": True, "deep": False, "flights": "ca1234, CA5678"}),
                         ["fetch", "--dry-run", "--flights", "CA1234,CA5678"])
        self.assertEqual(J.build_argv("export", "s", {"run": "2026-11-26_0814"}),
                         ["--season", "s", "export", "--run", "2026-11-26_0814"])
        self.assertEqual(J.build_argv("test", None, {"verbose": True}), ["test", "--verbose"])
        for cmd, season, opts in [("serve", None, {}), ("web-user", None, {}), ("status", None, {"deep": True}),
                                  ("fetch", None, {"dry_run": "yes"}), ("fetch", None, {"flights": "CA1;rm -rf"}),
                                  ("export", None, {"run": "../x"}), ("status", "../etc", {}),
                                  ("fetch", None, {"boards": "--deep"}), ("status", "-x", {})]:
            with self.assertRaises(ValueError, msg=(cmd, season, opts)):
                J.build_argv(cmd, season, opts)


class TestRun(JobTestCase):
    def test_run_log_progress_rundir(self):
        job = self.jm.start("status", "S1", {}, user="alice")
        self.assertEqual(job["status"], "running")
        done = self.jm.wait(job["id"], 30)
        self.assertEqual((done["status"], done["rc"], done["user"]), ("done", 0, "alice"))
        self.assertEqual(done["progress"], {"done": 3, "total": 10, "elapsed": "0:01", "eta": "0:05"})
        self.assertEqual(done["run_dir"], "2099-01-01_0000")
        log = self.jm.read_log(job["id"], 0)
        self.assertIn("hello 状态", log["text"])
        self.assertIn("args --season S1 status", log["text"])
        self.assertTrue(log["text"].startswith("$ vcca_plan.py --season S1 status"))
        self.assertIn("[任务完成]", log["text"])
        again = self.jm.read_log(job["id"], log["next"])
        self.assertEqual(again["text"], "")
        part = self.jm.read_log(job["id"], 5)
        self.assertEqual(part["text"], log["text"].encode("utf-8")[5:].decode("utf-8"))
        rec = json.loads((self.dir / "state" / "jobs" / f"{job['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["status"], "done")
        self.assertEqual([j["id"] for j in self.jm.list()], [job["id"]])
        self.assertIsNone(self.jm.read_log("../../etc", 0))
        self.assertIsNone(self.jm.get("nope"))

    def test_failure_exit_code(self):
        job = self.jm.start("test", None, {})
        done = self.jm.wait(job["id"], 30)
        self.assertEqual((done["status"], done["rc"]), ("failed", 3))

    def test_one_job_at_a_time(self):
        job = self.jm.start("build", "S1", {})
        with self.assertRaises(J.JobBusy) as cm:
            self.jm.start("status", None, {})
        self.assertEqual(cm.exception.job["id"], job["id"])
        self.assertTrue(self.jm.busy())
        # let the child really start before tearDown cancels it: on Windows a venv python.exe is a launcher, and
        # terminating it during its start-up can leave the real interpreter running (log still open -> tearDown's
        # rmtree fails with WinError 32 after ~20 s; seen ~1 run in 25)
        t0 = time.time()
        while "tick 0" not in self.jm.read_log(job["id"], 0)["text"] and time.time() - t0 < 20:
            time.sleep(0.05)

    def test_cancel(self):
        job = self.jm.start("fetch", "S1", {"dry_run": True})
        t0 = time.time()
        while "tick 3" not in (self.jm.read_log(job["id"], 0)["text"]) and time.time() - t0 < 20:
            time.sleep(0.1)
        self.jm.cancel(job["id"])
        done = self.jm.wait(job["id"], 30)
        self.assertEqual(done["status"], "cancelled")
        self.assertTrue(done["cancel_requested"])
        self.assertIsNone(self.jm.current())
        text = self.jm.read_log(job["id"], 0)["text"]
        self.assertIn("请求中断", text)
        self.assertIn("[任务已中断]", text)
        self.assertLess(time.time() - t0, 30)


class TestPlatformBranches(unittest.TestCase):
    def test_posix_interrupt_uses_killpg_sigint(self):
        proc = mock.Mock(pid=4321)
        with mock.patch.object(J.os, "killpg", create=True) as killpg:
            self.assertEqual(J.send_interrupt(proc), "SIGINT")
            killpg.assert_called_once_with(4321, signal.SIGINT)
            self.assertEqual(J.popen_kwargs(), {"start_new_session": True})
        proc.send_signal.assert_not_called()

    def test_without_killpg_terminates(self):
        proc = mock.Mock(pid=1)
        with mock.patch.object(J.os, "killpg", None, create=True):
            self.assertEqual(J.send_interrupt(proc), "terminate")
        proc.terminate.assert_called_once_with()

    def test_interrupt_failure_falls_back_to_terminate(self):
        with tempfile.TemporaryDirectory() as t:
            jm = J.JobManager(Path(t), script=Path(t) / "x.py", grace_s=0.1, kill_s=0.1)
            proc = mock.Mock(pid=1)
            proc.wait.side_effect = [subprocess.TimeoutExpired("x", 0.1), 0]
            with jm._lock:
                jm._current = {"id": "20990101-000000-abcd", "proc": proc, "thread": None}
                jm._save({"id": "20990101-000000-abcd", "status": "running", "started": 1})
            with mock.patch.object(J, "send_interrupt", side_effect=OSError("no console")):
                jm.cancel("20990101-000000-abcd")
            proc.terminate.assert_called()
            for _ in range(50):
                if proc.wait.call_count >= 2:
                    break
                time.sleep(0.05)

    def test_recovery_marks_running_as_interrupted(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "jobs"
            d.mkdir()
            (d / "20990101-000000-abcd.json").write_text(json.dumps(
                {"id": "20990101-000000-abcd", "cmd": "fetch", "status": "running", "started": 1}), encoding="utf-8")
            jm = J.JobManager(Path(t), script=Path(t) / "x.py")
            self.assertEqual(jm.get("20990101-000000-abcd")["status"], "interrupted")
            self.assertIsNone(jm.current())


class TestCli(unittest.TestCase):
    def test_serve_is_not_a_job(self):
        import vcca_plan

        self.assertIn("serve", vcca_plan.COMMANDS)
        self.assertNotIn("serve", J.COMMANDS)
        self.assertFalse(hasattr(vcca_plan, "_sigbreak_to_interrupt"))


if __name__ == "__main__":
    unittest.main()
