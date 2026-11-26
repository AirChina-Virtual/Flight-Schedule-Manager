"""Structured ``status``: ``collect_status(season)`` -> dict (web app ``/api/status``), ``print_status(d)`` -> the CLI
text (``vcca_plan.py status``).

Times are UNIX seconds (``mtime``) plus the CLI's local-time display strings (``mtime_fmt``); the web front end
formats the epoch itself with an explicit time zone.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from . import config as C


def fmt_mtime(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _file_info(p: Path) -> dict[str, Any]:
    if not p.is_file():
        return {"path": str(p), "name": p.name, "exists": False}
    st = p.stat()
    return {"path": str(p), "name": p.name, "exists": True, "size": st.st_size, "size_fmt": fmt_size(st.st_size),
            "mtime": st.st_mtime, "mtime_fmt": fmt_mtime(st.st_mtime)}


def collect_status(season: str | Path | None = None) -> dict[str, Any]:
    """Everything ``status`` shows, as plain JSON-able data. Never raises for data problems: a config error gives
    ``{'ok': False, 'error': str, 'errors': [...]}``; read failures are reported per section."""
    out: dict[str, Any] = {"ok": True}
    try:
        cfg = C.load_config(season, strict=False)
    except C.ConfigError as e:
        return {"ok": False, "error": str(e), "errors": list(e.errors)}
    out["season"] = cfg.season_name
    out["config"] = {
        "season_label": cfg.season_label, "season_name": cfg.season_name, "season_file": str(cfg.season_file),
        "season_file_exists": cfg.season_file.is_file(), "settings_file": str(cfg.settings_file),
        "settings_file_exists": bool(cfg.settings_file and cfg.settings_file.is_file()), "home": str(cfg.home),
        "start_date": cfg.start_date.isoformat(), "end_date": cfg.end_date.isoformat(),
        "dates_source": cfg.dates_source, "plan_tz_offset_min": cfg.plan_tz_offset_min,
        "target_ops": list(cfg.target_ops), "cargo_ops": list(cfg.cargo_ops),
        "effective_time": cfg.effective_time(), "effective_time_mode": cfg.effective_time_mode,
        "airport_grades": len(cfg.airport_rules.airport_grade),
        "airport_exclusions": len(cfg.airport_rules.fleet_exclusions),
        "fleet_map_pax": len(cfg.fleet_rules.ac_to_fleet_pax), "fleet_map_cargo": len(cfg.fleet_rules.ac_to_fleet_cargo),
        "warnings": list(cfg.warnings),
    }

    # ---- CAAC plan
    files = [_file_info(p) for p in cfg.plan_files]
    plan: dict[str, Any] = {"files": files, "ok": all(f["exists"] for f in files)}
    if plan["ok"]:
        from . import caac

        try:
            pd = caac.load_plan(cfg)
            groups = caac.group_legs(pd.legs, dict(cfg.airport_alias))
            gaps = caac.detect_rotation_gaps(pd.legs, cfg.number_prefix)
            by_op: dict[str, int] = {}
            for leg in pd.legs:
                by_op[leg.op] = by_op.get(leg.op, 0) + 1
            plan.update({
                "rows": len(pd.rows), "rows_by_file": dict(pd.files), "legs": len(pd.legs),
                "legs_by_op": sorted(by_op.items(), key=lambda kv: -kv[1]), "numbers": len(pd.numbers),
                "groups": len(groups), "single_day": sum(leg.single_day for leg in pd.legs), "gaps": len(gaps),
                "fd23": pd.fd23_mode, "fd24": pd.fd24_mode,
            })
        except Exception as e:  # noqa: BLE001 - CLI let this raise; keep it visible but structured
            plan["error"] = f"{type(e).__name__}: {e}"
    out["plan"] = plan

    # ---- VAMSYS
    vs_info: dict[str, Any] = _file_info(cfg.vamsys_db)
    if vs_info["exists"]:
        from . import vamsys

        try:
            vs = vamsys.load_vamsys(cfg)
            c = vs.counts()
            unrated = sorted(a for a in vs.airports if not cfg.airport_rules.is_rated(a))
            vs_info.update({"ok": True, "workspace_name": vs.workspace_name, "revision": vs.revision,
                            "storage_revision": vs.storage_revision, "counts": c, "unrated": unrated})
        except Exception as e:  # noqa: BLE001 - status must never crash
            vs_info.update({"ok": False, "error": str(e)})
    else:
        vs_info["ok"] = False
    out["vamsys"] = vs_info

    # ---- credentials (presence only)
    cs = C.credential_status(cfg)
    out["credentials"] = {"source": cs["source"],
                          "checked": [{"path": p, "user": hu, "password": hp} for p, hu, hp in cs["checked"]]}

    # ---- FR24 cache
    cache: dict[str, Any] = {"dir": str(cfg.cache_dir), "db": _file_info(cfg.cache_db), "stats": None}
    if cache["db"]["exists"]:
        try:
            from .fr24_store import FR24Store
            from .report_summary import fmt_ts

            with FR24Store.open(cfg, readonly=True) as st:
                s = st.stats(cfg)
            cache["stats"] = {"rows": s.rows, "last_fetch_at": s.last_fetch_at,
                              "last_fetch_fmt": fmt_ts(s.last_fetch_at) or "",
                              "targets_by_priority": dict(s.targets_by_priority), "stale_targets": s.stale_targets}
        except Exception as e:  # noqa: BLE001
            cache["stats_error"] = str(e)
    out["cache"] = cache

    # ---- runs
    runs = cfg.list_runs()
    out["runs"] = {"count": len(runs), "output_root": str(cfg.output_root),
                   "latest": runs[-1].name if runs else None,
                   "latest_has_decisions": bool(runs) and (runs[-1] / "decisions.json").is_file(),
                   "latest_with_decisions": (cfg.latest_run().name if cfg.latest_run() else None)}
    return out


def _ok(flag: bool) -> str:
    return "OK " if flag else "!! "


def print_status(d: dict[str, Any], print=print) -> int:  # noqa: A002 - injectable for tests
    """Print ``collect_status`` output exactly like the original CLI. Returns the exit code."""
    print("=== 配置 ===")
    if not d.get("ok"):
        print(d["error"])
        return 1
    c = d["config"]
    print(f"数据目录    {c['home']}")
    print(f"全站设置    {c['settings_file']}" + ("" if c["settings_file_exists"] else "  (不存在，全部用默认值)"))
    print(f"航季        {c['season_label']}  [{c['season_name']}]  {c['season_file']}"
          + ("" if c["season_file_exists"] else "  (无 season.toml，用默认值)"))
    print(f"起止        {c['start_date']} ~ {c['end_date']}  ({'配置' if c['dates_source'] == 'config' else '由FD23/FD24推导'})"
          f"  计划时区 UTC{c['plan_tz_offset_min'] / 60:+g}")
    print(f"航司        {','.join(c['target_ops'])}  (货运 {','.join(c['cargo_ops'])})")
    print(f"生效时间    {c['effective_time']} UTC  ({c['effective_time_mode']})")
    print(f"规则        机场等级 {c['airport_grades']} 个, 排除 {c['airport_exclusions']} 个;"
          f" 机型映射 {c['fleet_map_pax']}+{c['fleet_map_cargo']}")
    for w in c["warnings"]:
        print(f"!! 警告     {w}")

    print("\n=== CAAC 计划 ===")
    pl = d["plan"]
    for f in pl["files"]:
        print(f"{_ok(f['exists'])}{f['name']}" + (f"  {f['size_fmt']}  {f['mtime_fmt']}" if f["exists"] else "  (缺失)"))
    if pl["ok"]:
        if "error" in pl:
            print(f"!! 读取失败: {pl['error']}")
        else:
            print(f"目标航司行  {pl['rows']}  ({', '.join(f'{k} {v}' for k, v in pl['rows_by_file'].items())})")
            print(f"航段        {pl['legs']}  ({', '.join(f'{k} {v}' for k, v in pl['legs_by_op'])})")
            print(f"班号        {pl['numbers']}   航段组 {pl['groups']}   单日行航段 {pl['single_day']}"
                  f"   去回程缺失段 {pl['gaps']}")
            print(f"FD23/FD24 众数 {pl['fd23']}/{pl['fd24']}")

    print("\n=== VAMSYS ===")
    v = d["vamsys"]
    if v["exists"]:
        if v.get("ok"):
            n = v["counts"]
            print(f"OK {v['path']}  ({v['size_fmt']}, {v['mtime_fmt']})")
            print(f"工作区      {v['workspace_name']}  revision {v['revision']}  StorageRevision {v['storage_revision']}")
            print(f"机场 {n['airports']}  机队 {n['fleets']}  飞机 {n['aircraft']}  航路 {n['routings']}")
            print(f"航线 {n['routes']}  (商业 {n['commercial_routes']}, 特殊 {n['special_routes']}, 已设End Date {n['routes_with_end_date']})")
            print("按呼号前缀  " + ", ".join(f"{k} {x}" for k, x in n["routes_by_op"].items()))
            if v["unrated"]:
                print(f"!! 未定级机场 {len(v['unrated'])}: {', '.join(v['unrated'])}")
            else:
                print("OK VAMSYS 机场均已定级")
        else:
            print(f"!! 读取失败: {v['error']}")
    else:
        print(f"!! 数据库不存在: {v['path']}")

    print("\n=== FR24 账号 ===")
    cr = d["credentials"]
    for x in cr["checked"]:
        hu, hp = x["user"], x["password"]
        mark = "OK " if (hu and hp) else ("-  " if not (hu or hp) else "!! ")
        print(f"{mark}{x['path']}: 账号 {'有' if hu else '无'}, 密码 {'有' if hp else '无'}")
    print(f"将使用      {({'WEB': '网页填写的账号', 'ENV': '环境变量'}).get(cr['source'], '（未找到账号，fetch 无法登录）')}")

    print("\n=== FR24 缓存 ===")
    ca = d["cache"]
    print(f"目录        {ca['dir']}")
    db = ca["db"]
    if db["exists"]:
        print(f"OK fr24_cache.db  {db['size_fmt']}  {db['mtime_fmt']}")
        s = ca.get("stats")
        if s is not None:
            print(f"   行 {s['rows']}  最后抓取 {s['last_fetch_fmt'] or '无'}  目标 {s['targets_by_priority']}"
                  f"  过期 {s['stale_targets']}")
        elif ca.get("stats_error"):
            print(f"   !! 统计失败: {ca['stats_error']}")
    else:
        print("-  fr24_cache.db 尚未创建（首次 fetch 时创建，或在网页数据页导入）")

    print("\n=== 运行记录 ===")
    r = d["runs"]
    if r["count"]:
        print(f"{r['count']} 次，最新 {r['latest']}" + ("（含 decisions.json）" if r["latest_has_decisions"] else ""))
    else:
        print(f"无（输出目录 {r['output_root']}）")
    return 0
