"""Test helpers for fr24_obs / evidence / fleets / reconcile tests: real-sample fixture world + synthetic builders.

Imported by the tests via ``tests._support``-style path insertion (this folder is not a package).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from vcca import caac
from vcca.evidence import build_all_evidence
from vcca.fr24_obs import build_observations
from vcca.models import PlanData, PlanLeg, VRoute
from vcca.reconcile import BuildInputs, reconcile
from vcca.util_time import days_to_fd3, fmt_hm, parse_hhmm, shift_days
from vcca.vamsys import VAirport, VamsysSnapshot

HERE = Path(__file__).resolve().parent
UTC = dt.timezone.utc
NOW = int(dt.datetime(2026, 11, 25, 12, 0, tzinfo=UTC).timestamp())


def ts(y, m, d, hh=0, mm=0) -> int:
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


def load_json(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ fixture world
def fixture_rows() -> list[dict]:
    return load_json("fr24_rows.json")


def fixture_legs() -> list[PlanLeg]:
    out = []
    for d in load_json("plan_legs.json"):
        d = dict(d)
        d["utc_days"] = frozenset(d["utc_days"])
        out.append(PlanLeg(**d))
    return out


def fixture_vamsys() -> VamsysSnapshot:
    v = load_json("vamsys.json")
    routes = []
    for d in v["routes"]:
        d = dict(d)
        d["days"] = frozenset(d["days"])
        d["fleet_ids"] = frozenset(d["fleet_ids"])
        routes.append(VRoute(**d))
    aps = {c: VAirport(code=a["code"], icao=a["icao"], iata=a["iata"], name=a["name"]) for c, a in v["airports"].items()}
    return VamsysSnapshot(workspace_id="fixture", workspace_name="fixture", revision=v["revision"],
                          storage_revision=None, db_path=Path("fixture.db"), airports=aps, fleets={}, aircraft=[],
                          routes=routes)


def inputs(cfg, legs, vamsys, rows, *, search=None, fetched=()) -> BuildInputs:
    plan = PlanData(rows=[{}] * len({l.src_row for l in legs}), legs=list(legs), files={})
    groups = caac.group_legs(plan.legs, dict(cfg.airport_alias))
    gaps = caac.detect_rotation_gaps(plan.legs, cfg.number_prefix)
    cao = {l.number for l in legs if cfg.is_cargo_op(l.op)} | {
        cfg.callsign_to_number(v.callsign) for v in vamsys.routes if cfg.is_cargo_op(v.op)}
    index = build_observations(cfg, rows, cao_numbers=cao, search_index=search, fetched_numbers=fetched)
    keys = set(groups) | {v.key for v in vamsys.commercial_routes}
    ev = build_all_evidence(sorted(keys), index, cfg)
    return BuildInputs(plan=plan, groups=groups, gaps=gaps, vamsys=vamsys, index=index, evidence=ev,
                       airport_names={}, source="test")


def fixture_inputs(cfg) -> BuildInputs:
    return inputs(cfg, fixture_legs(), fixture_vamsys(), fixture_rows(), search=load_json("search_index.json"))


def run(cfg, inp: BuildInputs, vamsys=None):
    return reconcile(cfg, inp.plan, inp.groups, inp.gaps, vamsys or inp.vamsys, inp.evidence, inp.index, now=NOW)


# ------------------------------------------------------------------------------------------------ synthetic
_ID = [0]


def row(number, callsign, orig, dest, dep_ts, block_min=120, *, actual=True, airline=None, ac="A320",
        reg="B-0001", source=None, status=None, fid=True, sched=True) -> dict:
    """One FlightRow. dep_ts is the scheduled departure (UTC epoch)."""
    _ID[0] += 1
    arr_ts = dep_ts + block_min * 60
    return {
        "id": f"t{_ID[0]:07x}" if fid else None, "number": number, "callsign": callsign,
        "airline_icao": airline if airline is not None else (callsign[:3] if callsign else None),
        "origin_icao": orig, "origin_iata": None, "dest_icao": dest, "dest_iata": None,
        "aircraft_code": ac, "registration": reg,
        "sched_dep": dep_ts if sched else None, "sched_arr": arr_ts if sched else None,
        "real_dep": dep_ts + 300 if actual else None, "real_arr": arr_ts + 300 if actual else None,
        "status_live": False, "status_text": "",
        "status_generic": status or ("landed" if actual else "scheduled"),
        "source": source or (f"flight:{number}" if number else "reg:B-0001"),
    }


def series(number, callsign, orig, dest, hhmm, days, start=(2026, 10, 26), end=(2026, 11, 24), **kw) -> list[dict]:
    """Rows on every date in [start, end] whose UTC weekday is in ``days`` at ``hhmm`` UTC."""
    h, m = map(int, hhmm.split(":"))
    d = dt.date(*start)
    out = []
    while d <= dt.date(*end):
        if d.weekday() in days:
            out.append(row(number, callsign, orig, dest, ts(d.year, d.month, d.day, h, m), **kw))
        d += dt.timedelta(days=1)
    return out


def leg(callsign, orig, dest, dep, arr, days, *, op=None, fd2="320", single=False, src_row=None, route_type=None,
        leg_no=1) -> PlanLeg:
    op = op or callsign[:3]
    dm, am = parse_hhmm(dep), parse_hhmm(arr)
    shift = 1 if dm + 480 >= 1440 else 0
    _ID[0] += 1
    r = src_row or _ID[0]
    return PlanLeg(op=op, callsign=callsign, number="CA" + callsign[3:], orig=orig, dest=dest, leg_no=leg_no,
                   is_multileg=False, bjt_days=days_to_fd3(shift_days(days, shift)), day_offset=0,
                   bjt_dep=fmt_hm(dm + 480), bjt_arr=fmt_hm(am + 480), utc_dep=dep, utc_arr=arr, utc_dep_min=dm,
                   utc_arr_min=am, utc_days=frozenset(days), fd2=fd2, fd19="", fd26="",
                   fd23="1025" if single else "1025", fd24="1025" if single else "0327", single_day=single,
                   route_type=route_type or ("cargo" if op == "CAO" else "scheduled"), plan_group="国内",
                   src_file="t.xls", src_row=r, src_ref=f"t.xls:{r}")


def vroute(rid, callsign, orig, dest, dep, arr, days, *, fleets=("26072",), end_date="", number=None, type_=None,
           cost_index="80") -> VRoute:
    dm, am = parse_hhmm(dep), parse_hhmm(arr)
    return VRoute(id=str(rid), op=callsign[:3], callsign=callsign, number=number or "CA" + callsign[3:], orig=orig,
                  dest=dest, type=type_ or ("cargo" if callsign.startswith("CAO") else "scheduled"),
                  dep=dep, arr=arr, dep_min=dm, arr_min=am, days=frozenset(days), fleet_ids=frozenset(fleets),
                  start_date="", end_date=end_date, cost_index=cost_index, hidden=False, tags="", is_special=False)


def snapshot(routes, airports=("ZBAA", "ZSPD", "ZGGG", "ZUUU", "ZSSS", "ZHHH", "ZBAD", "ESSA", "EBLG", "KORD",
                               "PANC", "ZSGS", "ZLLL")) -> VamsysSnapshot:
    aps = {c: VAirport(code=c, icao=c, iata="", name=c) for c in airports}
    return VamsysSnapshot(workspace_id="synth", workspace_name="synth", revision=1, storage_revision=1,
                          db_path=Path("synth.db"), airports=aps, fleets={}, aircraft=[], routes=list(routes))
