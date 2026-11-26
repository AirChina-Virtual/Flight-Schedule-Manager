"""Build the test data directory ``tests/fixtures/home`` (a small ``$VCCA_HOME``) from a real data directory.

    python tools/make_fixtures.py [REAL_HOME]          # default F:\\vCCA\\dev_home (or $VCCA_TEST_HOME)

The real home is only READ (SQLite opened with mode=ro, files copied). The output directory is deleted and rebuilt,
so the script can be re-run whenever the samples should be refreshed. Layout written::

    tests/fixtures/home/
      settings.toml                    copy of REAL_HOME/settings.toml
      rules/                           copy of the repo rules/ (what a fresh deploy gets)
      seasons/<SEASON>/season.toml     REAL season.toml + plan_files limited to the small HK/Macau/Taiwan file
      seasons/<SEASON>/<PLAN_FILE>     the real CAAC .xls (236 KB)
      seasons/<SEASON>/flightopsdesk_csv/
                                       FlightOpsDesk example CSVs, header + first rows, bytes kept (CRLF)
      vamsys/workspaces.db             slim copy (vamsys.slim_copy: no secrets), vCCA workspace only, collection
                                       in use only; airports/fleets/aircraft/routings complete (aircraft raw JSON
                                       blanked), routes trimmed to the plan + a few special / unrelated ones
      cache/fr24_cache.db              rows / targets / fetch log / search queries for the plan flights, a few of
                                       their registrations and the HK/Macau/Taiwan airport boards
      fixture.json                     what was cut + counts the tests assert on (generated, do not edit)

Never copied: credentials, FR24 session files, state/ (users, sessions, jobs), output/, other workspaces.
The files are committed byte-for-byte (.gitattributes: -text), so a Linux checkout sees the same CRLF CSVs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT = REPO / "tests" / "fixtures" / "home"
SEASON = "2026-27-1"
PLAN_FILE = "国内航司港澳台航班计划（三字码）.xls"
DEFAULT_REAL_HOME = os.environ.get("VCCA_TEST_HOME") or r"F:\vCCA\dev_home"
HKMT_PREFIXES = ("VH", "VM", "RC")   # ICAO prefixes of Hong Kong, Macau, Taiwan
EXAMPLE_ROWS = 50      # rows kept per FlightOpsDesk example CSV (test_export_csv compares the first 50)
N_REGS = 6                 # registrations (with their reg-query rows) kept in the cache
N_SPECIAL_ROUTES = 12      # special (training / repositioning / tagged / other-op) routes kept besides the plan routes
N_OTHER_ROUTES = 24        # unrelated commercial routes (not in the plan), so reconcile also sees VAMSYS-only routes


def write_lf(path: Path, text: str) -> None:
    """Text files with LF line endings (read_text already turned CRLF into LF), whatever the checkout uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def qmarks(xs) -> str:
    return ",".join("?" * len(xs))


def build_vamsys(real: Path, dst: Path, cfg, plan_callsigns: set[str], plan_airports: set[str]) -> dict:
    from vcca import vamsys as V

    with tempfile.TemporaryDirectory() as t:
        slim = Path(t) / "slim.db"
        dropped = V.slim_copy(real, slim)
        con = sqlite3.connect(slim)
        try:
            ws, coll = cfg.workspace_id, cfg.collection
            con.execute("DELETE FROM workspaces WHERE id<>?", (ws,))
            con.execute("DELETE FROM workspace_revisions WHERE workspace_id<>?", (ws,))
            con.execute("DELETE FROM resource_rows WHERE workspace_id<>? OR collection<>?", (ws, coll))
            keep: list[str] = []
            special = other = 0
            for local_id, js in con.execute("SELECT local_id, json FROM resource_rows WHERE resource=? "
                                            "ORDER BY ordinal", (V.RES_ROUTES,)).fetchall():
                r = V.parse_route(json.loads(js), cfg.target_ops)
                if r.callsign in plan_callsigns or r.orig in plan_airports and r.dest in plan_airports and (
                        r.orig[:2] in HKMT_PREFIXES or r.dest[:2] in HKMT_PREFIXES):
                    keep.append(local_id)
                elif r.is_special and special < N_SPECIAL_ROUTES:
                    keep.append(local_id)
                    special += 1
                elif not r.is_special and other < N_OTHER_ROUTES and int(r.id or 0) % 97 == 0:
                    keep.append(local_id)
                    other += 1
            con.execute("CREATE TEMP TABLE keep(local_id TEXT PRIMARY KEY)")
            con.executemany("INSERT INTO keep VALUES(?)", [(x,) for x in keep])
            con.execute("DELETE FROM resource_rows WHERE resource=? AND local_id NOT IN (SELECT local_id FROM keep)",
                        (V.RES_ROUTES,))
            # aircraft: the loader reads only Fields; their RawApiJson (~60% of the DB) is blanked to keep it small
            con.execute("UPDATE resource_rows SET json=json_set(json, '$.RawApiJson', '') WHERE resource=?",
                        (V.RES_AIRCRAFT,))
            con.commit()
            con.execute("VACUUM")
            tables = [n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        finally:
            con.close()
        assert "secrets" not in tables, tables
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(slim, dst)
    vs = V.load_vamsys(cfg, db_path=dst)
    c = vs.counts()
    return {"dropped_tables": dropped, "tables": sorted(tables), "counts": {k: v for k, v in c.items()},
            "special_routes_kept": special, "other_routes_kept": other}


def build_cache(real: Path, dst: Path, numbers: set[str], callsigns: set[str], airports: set[str]) -> dict:
    from vcca import fr24_store as FS

    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{dst.as_posix()}", uri=True)          # uri: needed for the mode=ro ATTACH
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.executescript(FS._SCHEMA)
        con.execute("ATTACH DATABASE ? AS src", (f"file:{real.as_posix()}?mode=ro",))
        nums, css = sorted(numbers), sorted(callsigns)
        flight_q = f"(number IN ({qmarks(nums)}) OR callsign IN ({qmarks(css)}))"
        # registrations that flew the plan flights: keep the N_REGS busiest (their reg-query rows come along)
        regs = [r for (r,) in con.execute(
            f"SELECT reg FROM src.flight_rows WHERE {flight_q} AND reg IS NOT NULL AND reg<>'' "
            "GROUP BY reg ORDER BY count(*) DESC, reg LIMIT ?", (*nums, *css, N_REGS))]
        hkmt = sorted(a for a in airports if a[:2] in HKMT_PREFIXES)            # boards: HK/Macau/Taiwan only
        boards = sorted(a for (a,) in con.execute(
            f"SELECT DISTINCT query FROM src.fetch_log WHERE kind='board' AND query IN ({qmarks(hkmt)})", hkmt))
        sources = [f"reg:{r}" for r in regs] + [f"board:{b}" for b in boards]
        con.execute(f"INSERT INTO main.flight_rows SELECT * FROM src.flight_rows WHERE {flight_q} "
                    f"OR source IN ({qmarks(sources) if sources else 'NULL'})", (*nums, *css, *sources))
        queries = nums + regs + boards
        con.execute(f"INSERT INTO main.targets SELECT * FROM src.targets WHERE query IN ({qmarks(queries)})", queries)
        con.execute(f"INSERT INTO main.fetch_log SELECT * FROM src.fetch_log WHERE query IN ({qmarks(queries)})",
                    queries)
        con.execute(f"INSERT INTO main.airport_info SELECT * FROM src.airport_info WHERE icao IN ({qmarks(airports)})",
                    sorted(airports))
        sq = [q for (q,) in con.execute("SELECT query FROM src.search_queries")
              if any(n.startswith(q) for n in nums)]
        con.execute(f"INSERT INTO main.search_queries SELECT * FROM src.search_queries WHERE query IN ({qmarks(sq)})",
                    sq)
        con.execute("INSERT INTO main.meta SELECT * FROM src.meta")
        con.commit()
        con.execute("DETACH DATABASE src")
        con.execute("VACUUM")
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("flight_rows", "targets", "fetch_log", "search_queries", "airport_info", "meta")}
    finally:
        con.close()
    return {"regs": regs, "boards": boards, "counts": counts}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("real_home", nargs="?", default=DEFAULT_REAL_HOME, help="real $VCCA_HOME (read only)")
    a = ap.parse_args(argv)
    real = Path(a.real_home).resolve()
    if not (real / "seasons" / SEASON / PLAN_FILE).is_file():
        ap.error(f"{real}: no seasons/{SEASON}/{PLAN_FILE}")
    if real == OUT.resolve():
        ap.error("source and output are the same directory")

    from vcca import caac
    from vcca.config import load_config
    from vcca.util_time import parse_hhmm

    real_cfg = load_config(SEASON, home=real)

    if OUT.exists():
        shutil.rmtree(OUT)
    sd = OUT / "seasons" / SEASON
    sd.mkdir(parents=True)
    write_lf(OUT / "settings.toml", (real / "settings.toml").read_text(encoding="utf-8"))
    for f in sorted((REPO / "rules").glob("*.toml")):
        write_lf(OUT / "rules" / f.name, f.read_text(encoding="utf-8"))
    shutil.copyfile(real / "seasons" / SEASON / PLAN_FILE, sd / PLAN_FILE)
    # FlightOpsDesk example CSVs (byte-format reference for test_export_csv): header + first EXAMPLE_ROWS rows,
    # bytes unchanged (CRLF, no BOM)
    for f in sorted((real / "seasons" / SEASON / "flightopsdesk_csv").glob("*.csv")):
        lines = f.read_bytes().split(b"\r\n")
        out = sd / "flightopsdesk_csv" / f.name
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(b"\r\n".join(lines[:EXAMPLE_ROWS + 1]) + (b"\r\n" if len(lines) > EXAMPLE_ROWS + 1 else b""))
    body = (real / "seasons" / SEASON / "season.toml").read_text(encoding="utf-8").rstrip("\n").split("\n", 1)[1]
    write_lf(sd / "season.toml", f"# 测试数据（tools/make_fixtures.py 生成，勿手改）：{SEASON} 航季，只含港澳台计划文件。\n"
             + body + f'\nplan_files = ["{PLAN_FILE}"]\n')

    cfg = load_config(SEASON, home=OUT, strict=False)      # vamsys/ and cache/ are written below
    plan = caac.load_plan(cfg)
    groups = caac.group_legs(plan.legs, dict(cfg.airport_alias))
    gaps = caac.detect_rotation_gaps(plan.legs, cfg.number_prefix)
    callsigns = {k[1] for k in groups}
    numbers = set(plan.numbers) | {cfg.callsign_to_number(c) for c in callsigns}
    airports = {a for k in groups for a in k[2:]}

    vam = build_vamsys(real_cfg.vamsys_db, OUT / "vamsys" / "workspaces.db", cfg, callsigns, airports)
    cache = build_cache(real_cfg.cache_db, OUT / "cache" / "fr24_cache.db", numbers, callsigns, airports)

    # micro-split: one group with >= 2 raw slots whose departures differ by at most 15 min (merged by calibration)
    micro = sorted(k for k, g in groups.items() if len({s.dep for s in g.raw_slots}) > 1
                   and max(parse_hhmm(s.dep) for s in g.raw_slots) - min(parse_hhmm(s.dep) for s in g.raw_slots) <= 15)
    manifest = {
        "generated_from": "real $VCCA_HOME (tools/make_fixtures.py)",
        "season": SEASON,
        "plan_file": PLAN_FILE,
        "plan": {"rows": len(plan.rows), "legs": len(plan.legs), "numbers": len(plan.numbers), "groups": len(groups),
                 "rotation_gaps": len(gaps), "fd23_fd24": [plan.fd23_mode, plan.fd24_mode],
                 "micro_split_example": list(micro[0]) if micro else None,
                 "micro_split_slots": [[s.dep, s.fd3] for s in groups[micro[0]].raw_slots] if micro else None},
        "vamsys": vam,
        "cache": cache,
    }
    write_lf(OUT / "fixture.json", json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"wrote {OUT} ({total / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
