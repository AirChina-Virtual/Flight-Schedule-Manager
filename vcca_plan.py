"""vCCA season flight-plan tool — single entry point.

    python vcca_plan.py                      text menu
    python vcca_plan.py status               config, plan files, VAMSYS, credentials, FR24 cache
    python vcca_plan.py fetch [--deep] [--flights CA1,CA2] [--regs B-1,B-2] [--boards ZBAA] [--search] [--dry-run]
    python vcca_plan.py build                compute -> output/<time>/decisions.json
    python vcca_plan.py export [--run NAME]  decisions.json -> xlsx / csv / html / summary
    python vcca_plan.py run [--deep]         fetch + build + export
    python vcca_plan.py test                 unittest discovery in tests/
    python vcca_plan.py serve [--host 127.0.0.1] [--port 8770] [--no-browser]   local web front end
    python vcca_plan.py web-user set NAME | list | delete NAME                   web front end accounts

Global: --season 2026-27-1 (default: latest folder under F:\\vCCA\\plan containing season.toml)
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import unittest
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

for _s in (sys.stdout, sys.stderr):  # console is cp1252 by default on this machine
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from vcca import config as C  # noqa: E402


def _sigbreak_to_interrupt(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


# The web app cancels a job on Windows with CTRL_BREAK_EVENT (Ctrl+C cannot reach a new process group); treat it as
# Ctrl+C so fetch saves its progress and exits cleanly. POSIX jobs get SIGINT, which already raises KeyboardInterrupt.
if hasattr(signal, "SIGBREAK"):
    try:
        signal.signal(signal.SIGBREAK, _sigbreak_to_interrupt)
    except (ValueError, OSError):  # not the main thread (imported by the web app)
        pass

EXIT_NOT_IMPLEMENTED = 2


def _csv_list(s: str | None) -> list[str] | None:
    return [x.strip().upper() for x in s.split(",") if x.strip()] if s else None


# ------------------------------------------------------------------ status -------------------------------------------
def collect_status(season: str | None = None) -> dict:
    """Structured status (the web app's /api/status); ``cmd_status`` prints the same data."""
    from vcca.status import collect_status as _collect

    return _collect(season)


def cmd_status(args) -> int:
    from vcca.status import print_status

    return print_status(collect_status(args.season))


# ------------------------------------------------------------------ pipeline commands --------------------------------
def _load_cfg(args):
    return C.load_config(args.season)


def cmd_fetch(args) -> int:
    from vcca import fr24_fetch

    cfg = _load_cfg(args)
    rep = fr24_fetch.fetch(cfg, deep=args.deep, flights=_csv_list(args.flights), regs=_csv_list(args.regs),
                           boards=_csv_list(args.boards), search=args.search, dry_run=args.dry_run)
    return 1 if rep.stopped_early else 0


def cmd_build(args) -> int:
    from vcca import reconcile

    cfg = _load_cfg(args)
    run_dir = reconcile.build(cfg)
    print(f"decisions.json -> {run_dir}")
    return 0


def cmd_export(args) -> int:
    """decisions.json of one run (default: latest) -> xlsx + FlightOpsDesk CSVs + report.html + summary.json."""
    from vcca import export_csv, export_html, export_xlsx, report_summary, vamsys
    from vcca.models import load_decisions

    cfg = _load_cfg(args)
    run_dir = cfg.resolve_run(getattr(args, "run", None))
    if run_dir is None or not (run_dir / "decisions.json").is_file():
        print(f"找不到含 decisions.json 的运行目录（{getattr(args, 'run', None) or '最新'}），请先 build。")
        return 1
    log = report_summary.setup_run_log(run_dir)
    log.info("export %s", run_dir)
    meta, decisions = load_decisions(run_dir / "decisions.json")
    vs = vamsys.load_vamsys(cfg)
    prev = [r for r in cfg.list_runs() if (r / "decisions.json").is_file() and r.name < run_dir.name]
    paths = [export_xlsx.export_xlsx(run_dir, meta, decisions, cfg, vs)]
    paths.append(export_html.export_html(run_dir, meta, decisions, cfg, previous_run_dir=prev[-1] if prev else None,
                                         vamsys=vs))
    csv_plan = export_csv.plan_csv(meta, decisions, cfg)
    warnings = list(csv_plan.warnings)
    rc = 0
    try:
        csvs = export_csv.export_csv(run_dir, meta, decisions, cfg, vs)
    except export_csv.CsvValidationError as e:
        log.error("%s", e)
        export_csv.remove_old_csvs(run_dir)  # never leave CSVs from an older export next to a failed one
        warnings.append(f"CSV 自检失败 {len(e.errors)} 条，未写出 CSV（详见 run.log）")
        csvs, rc = [], 1
    paths += csvs
    summary = report_summary.summarize(meta, decisions, csv_counts=export_csv.csv_counts(csvs),
                                       files=paths, warnings=warnings, vamsys=vs)
    paths.append(report_summary.write_summary(run_dir, meta, decisions, summary=summary))
    summary["files"] = [p.name for p in paths]
    report_summary.print_summary(summary)
    print(f"\n输出目录 {run_dir}")
    log.info("export done: %s", ", ".join(p.name for p in paths))
    return rc


def cmd_run(args) -> int:
    """fetch + build + export (the new run directory)."""
    from vcca import reconcile

    args.flights = args.regs = args.boards = None
    args.search = args.dry_run = False
    rc = cmd_fetch(args)
    if rc:
        print("fetch 未完成（已保存进度）；继续用现有缓存 build。")
    cfg = _load_cfg(args)
    run_dir = reconcile.build(cfg)
    print(f"decisions.json -> {run_dir}")
    args.run = str(run_dir)
    return cmd_export(args)


def cmd_test(args) -> int:
    suite = unittest.defaultTestLoader.discover(str(WORK_DIR / "tests"), pattern="test_*.py",
                                                top_level_dir=str(WORK_DIR))
    res = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if res.wasSuccessful() else 1


def cmd_serve(args) -> int:
    """Local web front end: stdlib wsgiref server with one thread per request (Linux servers use gunicorn)."""
    import threading
    import webbrowser
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    if args.plan_root:
        os.environ["VCCA_PLAN_ROOT"] = str(Path(args.plan_root).resolve())
    if args.state_dir:
        os.environ["VCCA_STATE_DIR"] = str(Path(args.state_dir).resolve())
    from vcca import webapp

    state = C.state_dir()
    if not webapp.load_users(state):
        print(f"还没有网页账号（{state / webapp.USERS_FILE}）。")
        if sys.stdin is not None and sys.stdin.isatty():
            name = input("现在创建账号，输入用户名（直接回车跳过）: ").strip()
            if name:
                _web_user_set(name)
        else:
            print("请先运行: vcca_plan.py web-user set <用户名>")

    class _Server(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = False

    class _Handler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):  # only errors and writes; polling GETs stay quiet
            if self.command != "GET" or not str(code).startswith(("2", "3")):
                super().log_request(code, size)

    app = webapp.create_app()
    try:
        httpd = make_server(args.host, args.port, app, server_class=_Server, handler_class=_Handler)
    except OSError as e:
        print(f"无法监听 {args.host}:{args.port}: {e}")
        return 1
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    print(f"vCCA 网页前端: {url}   （数据目录 {C.plan_root()}，状态目录 {state}）  Ctrl+C 停止")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("!! 注意：监听非本机地址，局域网内其他电脑也能访问（只有 HTTP，没有加密）。")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中…")
    finally:
        httpd.server_close()
        app.shutdown()
    return 0


def _web_user_set(name: str) -> int:
    import getpass

    from vcca import webapp

    if not webapp.USERNAME_RE.fullmatch(name):
        print("用户名只能包含字母、数字、. _ -（1–32 个字符）")
        return 1
    pw = getpass.getpass(f"{name} 的新密码: ")
    if len(pw) < 8:
        print("密码至少 8 个字符。")
        return 1
    if getpass.getpass("再输入一次: ") != pw:
        print("两次输入不一致。")
        return 1
    created = webapp.set_user(C.state_dir(), name, pw)
    print(f"{'已创建' if created else '已修改密码'}: {name}  （{C.state_dir() / webapp.USERS_FILE}）")
    return 0


def cmd_web_user(args) -> int:
    from vcca import webapp

    if args.action == "set":
        if not args.name:
            print("用法: web-user set <用户名>")
            return 1
        return _web_user_set(args.name)
    if args.action == "delete":
        ok = webapp.delete_user(C.state_dir(), args.name or "")
        print("已删除" if ok else "没有这个账号")
        return 0 if ok else 1
    users = webapp.load_users(C.state_dir())
    for n, u in sorted(users.items()):
        print(f"{n}  创建 {u.get('created', '-')}  修改 {u.get('updated', '-')}")
    if not users:
        print("（无账号）")
    return 0


COMMANDS = {"status": cmd_status, "fetch": cmd_fetch, "build": cmd_build, "export": cmd_export,
            "run": cmd_run, "test": cmd_test, "serve": cmd_serve, "web-user": cmd_web_user}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vcca_plan.py", description="vCCA 航季航班计划工具（CAAC 计划 + FR24 vs VAMSYS）")
    p.add_argument("--season", help="航季文件夹名或路径（默认：最新含 season.toml 的文件夹）")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="配置、文件、账号、缓存覆盖率与新鲜度")
    f = sub.add_parser("fetch", help="增量抓取 FR24")
    f.add_argument("--deep", action="store_true", help="全机队注册号扫描")
    f.add_argument("--flights", help="只抓这些班号（逗号分隔）")
    f.add_argument("--regs", help="只抓这些注册号（逗号分隔）")
    f.add_argument("--boards", help="抓这些机场看板（逗号分隔 ICAO）")
    f.add_argument("--search", action="store_true", help="刷新搜索索引")
    f.add_argument("--dry-run", action="store_true", help="只列出将抓的目标与请求数")
    sub.add_parser("build", help="计算 → output/<时间>/decisions.json")
    e = sub.add_parser("export", help="decisions.json → xlsx / csv / html / summary")
    e.add_argument("--run", help="运行目录名（默认最新）")
    r = sub.add_parser("run", help="fetch + build + export")
    r.add_argument("--deep", action="store_true")
    t = sub.add_parser("test", help="运行规则测试")
    t.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("serve", help="本机网页前端（默认 http://127.0.0.1:8770/）")
    s.add_argument("--host", default="127.0.0.1", help="监听地址（默认只允许本机访问）")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.add_argument("--plan-root", help=argparse.SUPPRESS)   # = VCCA_PLAN_ROOT (sandbox / tests)
    s.add_argument("--state-dir", help=argparse.SUPPRESS)   # = VCCA_STATE_DIR
    w = sub.add_parser("web-user", help="网页前端账号：set <名字>（创建/改密码）、list、delete <名字>")
    w.add_argument("action", choices=["set", "list", "delete"])
    w.add_argument("name", nargs="?")
    return p


def dispatch(args) -> int:
    try:
        return COMMANDS[args.cmd](args)
    except NotImplementedError as e:
        what = str(e) or "该功能"
        print(f"\n[尚未实现] {what}\n这一步属于后续重构阶段（见 重构计划.md 第 9 节），当前可用：status、test。")
        return EXIT_NOT_IMPLEMENTED
    except C.ConfigError as e:
        print(str(e))
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


MENU = [
    ("状态检查 (status)", ["status"]),
    ("增量抓取 FR24 (fetch)", ["fetch"]),
    ("抓取预览 (fetch --dry-run)", ["fetch", "--dry-run"]),
    ("计算决策 (build)", ["build"]),
    ("导出最新结果 (export)", ["export"]),
    ("全流程 (run)", ["run"]),
    ("运行测试 (test)", ["test"]),
    ("网页前端 (serve)", ["serve"]),
]


def menu(season: str | None) -> int:
    parser = build_parser()
    while True:
        print("\n==== vCCA 航季计划工具 ====")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}. {label}")
        print("  0. 退出")
        try:
            choice = input("选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("0", "q", ""):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            print("无效选择。")
            continue
        argv = (["--season", season] if season else []) + MENU[int(choice) - 1][1]
        dispatch(parser.parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        return menu(args.season)
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
