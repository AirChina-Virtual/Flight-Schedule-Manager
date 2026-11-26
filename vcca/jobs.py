"""Background jobs for the web app: one ``vcca_plan.py`` subprocess at a time, log + state on disk.

    jm = JobManager(state_dir)                       # STATE_DIR/jobs/<id>.log + <id>.json
    job = jm.start("fetch", "2026-27-1", {"dry_run": True}, user="admin")
    jm.read_log(job["id"], since=0)                  # {'text', 'next', 'job'}
    jm.cancel(job["id"])                             # SIGINT to the process group, then terminate, then kill

* Only one job runs at a time (``JobBusy`` -> HTTP 409). Commands and options are whitelisted (``build_argv``).
* The child runs ``python -u vcca_plan.py --season S <cmd> ...`` with ``PYTHONIOENCODING=utf-8``; stdout+stderr are
  merged and appended line by line to the log. Progress ``[done/total] … 已用 X  预计剩余 Y`` (fr24_fetch) and the
  produced run folder (``decisions.json -> …`` / ``输出目录 …``) are parsed from the output.
* Interrupt (Linux server): the child starts a new session and gets SIGINT on its process group (``os.killpg``), so
  fetch saves its progress like Ctrl+C. If it is still alive after ``grace_s`` it is terminated, then killed.
  Without ``os.killpg`` (Windows dev machines) cancel simply terminates the child.
* Records survive restarts; a job left "running" by a dead server is marked "interrupted" on startup.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config as C

COMMANDS = ("status", "fetch", "build", "export", "run", "test")
#: option -> (kind, CLI flag); kind 'bool' adds the flag, 'list' adds flag + comma list, 'run' adds flag + run name
OPTIONS: dict[str, dict[str, tuple[str, str]]] = {
    "status": {},
    "fetch": {"deep": ("bool", "--deep"), "dry_run": ("bool", "--dry-run"), "search": ("bool", "--search"),
              "flights": ("list", "--flights"), "regs": ("list", "--regs"), "boards": ("list", "--boards")},
    "build": {},
    "export": {"run": ("run", "--run")},
    "run": {"deep": ("bool", "--deep")},
    "test": {"verbose": ("bool", "--verbose")},
}
SEASON_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.\-]{0,47}")
RUN_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{4}")
LIST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{0,11}(,[A-Za-z0-9][A-Za-z0-9\-]{0,11}){0,199}")
JOB_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\].*?已用\s+(\S+)\s+预计剩余\s+(\S+)")
RUN_DIR_RES = (re.compile(r"decisions\.json -> (.+?)\s*$"), re.compile(r"^输出目录 (.+?)\s*$"))
ACTIVE = ("running",)


class JobBusy(RuntimeError):
    """Another job is running."""

    def __init__(self, job: dict):
        super().__init__(f"已有任务在运行: {job.get('cmd')} ({job.get('id')})")
        self.job = job


def build_argv(cmd: str, season: str | None, opts: dict[str, Any] | None) -> list[str]:
    """Validated CLI arguments after ``vcca_plan.py`` (raises ValueError on anything not whitelisted)."""
    if cmd not in COMMANDS:
        raise ValueError(f"不支持的命令: {cmd!r}")
    argv: list[str] = []
    if season:
        if not SEASON_RE.fullmatch(str(season)):
            raise ValueError(f"航季名不合法: {season!r}")
        argv += ["--season", str(season)]
    argv.append(cmd)
    allowed = OPTIONS[cmd]
    for k, v in (opts or {}).items():
        if k not in allowed:
            raise ValueError(f"{cmd} 不支持选项 {k!r}")
        kind, flag = allowed[k]
        if kind == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"选项 {k} 应为 true/false")
            if v:
                argv.append(flag)
        elif v in (None, ""):
            continue
        elif kind == "list":
            val = re.sub(r"[\s,]+", ",", str(v).strip()).strip(",").upper()
            if not LIST_RE.fullmatch(val):
                raise ValueError(f"选项 {k} 应为逗号分隔的代码（字母、数字、-）")
            argv += [flag, val]
        elif kind == "run":
            if not RUN_RE.fullmatch(str(v)):
                raise ValueError(f"运行目录名不合法: {v!r}")
            argv += [flag, str(v)]
    return argv


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def send_interrupt(proc: subprocess.Popen) -> str:
    """Ask the child (and its process group) to stop like Ctrl+C. Returns the method used."""
    killpg = getattr(os, "killpg", None)
    if killpg is None:                     # no process groups (Windows dev machine): plain terminate
        proc.terminate()
        return "terminate"
    killpg(proc.pid, signal.SIGINT)
    return "SIGINT"


def popen_kwargs() -> dict[str, Any]:
    """New session (= process group) so the interrupt reaches the child and its children but never the server."""
    return {"start_new_session": True}


class JobManager:
    def __init__(self, state_dir: Path | None = None, *, python: str | None = None, script: Path | None = None,
                 grace_s: float = 10.0, kill_s: float = 5.0, env: dict[str, str] | None = None):
        self.dir = Path(state_dir or C.state_dir()) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.python = python or sys.executable
        self.script = Path(script or C.WORK_DIR / "vcca_plan.py")
        self.grace_s, self.kill_s = grace_s, kill_s
        self.extra_env = dict(env or {})
        self._lock = threading.RLock()
        self._current: dict[str, Any] | None = None      # {'id', 'proc', 'thread', 'cancel'}
        self._recover()

    # ------------------------------------------------------------------ records
    def _json_path(self, jid: str) -> Path:
        return self.dir / f"{jid}.json"

    def log_path(self, jid: str) -> Path:
        return self.dir / f"{jid}.log"

    def _save(self, job: dict) -> None:
        p = self._json_path(job["id"])
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def _load(self, jid: str) -> dict | None:
        if not JOB_ID_RE.fullmatch(jid or ""):
            return None
        try:
            return json.loads(self._json_path(jid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _recover(self) -> None:
        for p in self.dir.glob("*.json"):
            try:
                job = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") in ACTIVE:
                job.update(status="interrupted", ended=job.get("ended") or _now(),
                           note="服务重启时任务仍在运行，已标记为中断")
                self._save(job)

    def _public(self, job: dict) -> dict:
        out = dict(job)
        out["started_iso"], out["ended_iso"] = _iso(job.get("started")), _iso(job.get("ended"))
        end = job.get("ended") or (_now() if job.get("status") in ACTIVE else job.get("started"))
        out["elapsed_s"] = round((end or 0) - (job.get("started") or 0), 1) if job.get("started") else 0
        try:
            out["log_size"] = self.log_path(job["id"]).stat().st_size
        except OSError:
            out["log_size"] = 0
        return out

    # ------------------------------------------------------------------ queries
    def current(self) -> dict | None:
        with self._lock:
            if self._current is None:
                return None
            job = self._load(self._current["id"])
            return self._public(job) if job else None

    def busy(self) -> bool:
        return self.current() is not None

    def get(self, jid: str) -> dict | None:
        job = self._load(jid)
        return self._public(job) if job else None

    def list(self, limit: int = 50) -> list[dict]:
        ids = sorted((p.stem for p in self.dir.glob("*.json") if JOB_ID_RE.fullmatch(p.stem)), reverse=True)
        out = []
        for jid in ids[:limit]:
            job = self._load(jid)
            if job:
                out.append(self._public(job))
        return out

    def read_log(self, jid: str, since: int = 0, max_bytes: int = 256 * 1024) -> dict | None:
        """Log text from byte offset ``since`` (whole lines only, up to ``max_bytes``) and the next offset."""
        job = self.get(jid)
        if job is None:
            return None
        text, nxt = "", since
        try:
            with open(self.log_path(jid), "rb") as f:
                f.seek(max(0, since))
                data = f.read(max_bytes)
            if data:
                cut = data.rfind(b"\n") + 1
                if cut == 0 and len(data) >= max_bytes:     # one enormous line: hand it over anyway
                    cut = len(data)
                done = job["status"] not in ACTIVE
                if done:                                     # finished: no partial line will grow any more
                    cut = len(data)
                text = data[:cut].decode("utf-8", errors="replace")
                nxt = since + cut
        except OSError:
            pass
        return {"text": text, "next": nxt, "job": job}

    # ------------------------------------------------------------------ control
    def start(self, cmd: str, season: str | None, opts: dict[str, Any] | None = None, *, user: str = "") -> dict:
        args = build_argv(cmd, season, opts)
        with self._lock:
            cur = self.current()
            if cur is not None:
                raise JobBusy(cur)
            jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
            argv = [self.python, "-u", str(self.script)] + args
            job = {"id": jid, "cmd": cmd, "season": season, "opts": opts or {}, "args": args, "user": user,
                   "status": "running", "rc": None, "started": _now(), "ended": None, "pid": None,
                   "progress": None, "run_dir": None, "cancel_requested": False}
            env = dict(os.environ)
            env.update(self.extra_env)
            env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
            logf = open(self.log_path(jid), "ab")
            logf.write(f"$ vcca_plan.py {' '.join(args)}\n".encode("utf-8"))
            logf.flush()
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        cwd=str(self.script.parent), env=env, **popen_kwargs())
            except OSError as e:
                logf.write(f"无法启动: {e}\n".encode("utf-8"))
                logf.close()
                job.update(status="failed", ended=_now(), note=str(e))
                self._save(job)
                return self._public(job)
            job["pid"] = proc.pid
            self._save(job)
            t = threading.Thread(target=self._pump, args=(job, proc, logf), name=f"job-{jid}", daemon=True)
            self._current = {"id": jid, "proc": proc, "thread": t}
            t.start()
            return self._public(job)

    def _pump(self, job: dict, proc: subprocess.Popen, logf) -> None:
        last_save = 0.0
        try:
            for raw in iter(proc.stdout.readline, b""):
                logf.write(raw)
                logf.flush()
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                changed = False
                m = PROGRESS_RE.search(line)
                if m:
                    job["progress"] = {"done": int(m.group(1)), "total": int(m.group(2)),
                                       "elapsed": m.group(3), "eta": m.group(4)}
                    changed = True
                for rx in RUN_DIR_RES:
                    m = rx.search(line)
                    if m:
                        job["run_dir"] = Path(m.group(1).strip()).name
                        changed = True
                if changed and _now() - last_save > 0.5:
                    with self._lock:
                        job["cancel_requested"] = (self._load(job["id"]) or {}).get("cancel_requested", False)
                        self._save(job)
                    last_save = _now()
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001 - never leave a job "running"
            logf.write(f"\n[任务管理] 读取输出失败: {e}\n".encode("utf-8"))
            rc = proc.poll()
        finally:
            proc.stdout.close()
        with self._lock:
            saved = self._load(job["id"]) or {}
            cancelled = saved.get("cancel_requested", False)
            job["cancel_requested"] = cancelled
            job["rc"] = rc
            job["ended"] = _now()
            job["status"] = "cancelled" if cancelled else ("done" if rc == 0 else "failed")
            tail = {"cancelled": "已中断", "done": "完成", "failed": f"失败（退出码 {rc}）"}[job["status"]]
            logf.write(f"\n[任务{tail}]\n".encode("utf-8"))
            logf.close()
            self._save(job)
            if self._current and self._current["id"] == job["id"]:
                self._current = None

    def cancel(self, jid: str) -> dict | None:
        """Interrupt the running job ``jid``; escalates to terminate/kill in the background."""
        with self._lock:
            if not self._current or self._current["id"] != jid:
                return self.get(jid)
            proc: subprocess.Popen = self._current["proc"]
            job = self._load(jid) or {}
            job["cancel_requested"] = True
            self._save(job)
        try:
            method = send_interrupt(proc)
        except (OSError, ValueError, AttributeError) as e:   # e.g. the process group is already gone
            method = f"interrupt failed ({e}); terminate"
            try:
                proc.terminate()
            except OSError:
                pass
        with open(self.log_path(jid), "ab") as f:
            f.write(f"\n[任务管理] 请求中断（{method}）\n".encode("utf-8"))
        threading.Thread(target=self._escalate, args=(jid, proc), daemon=True).start()
        return self.get(jid)

    def _escalate(self, jid: str, proc: subprocess.Popen) -> None:
        for action, wait in (("terminate", self.grace_s), ("kill", self.kill_s)):
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                try:
                    with open(self.log_path(jid), "ab") as f:
                        f.write(f"[任务管理] 子进程未退出，{action}\n".encode("utf-8"))
                    getattr(proc, action)()
                except OSError:
                    pass

    def wait(self, jid: str, timeout: float = 60.0) -> dict | None:
        """Block until job ``jid`` is no longer running (tests / shutdown)."""
        with self._lock:
            t = self._current["thread"] if self._current and self._current["id"] == jid else None
        if t is not None:
            t.join(timeout)
        return self.get(jid)

    def shutdown(self, timeout: float = 15.0) -> None:
        cur = self.current()
        if cur:
            self.cancel(cur["id"])
            self.wait(cur["id"], timeout)


__all__ = ["JobManager", "JobBusy", "build_argv", "send_interrupt", "popen_kwargs", "COMMANDS", "OPTIONS"]
