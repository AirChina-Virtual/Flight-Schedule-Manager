"""Small synthetic world for the step-6 exporter tests (no network, no workspaces.db).

``world()`` returns (meta, decisions, vamsys) covering every routing rule of export_csv:
  * one decision per DecisionCategory (iterated from the enum, so new members are covered automatically when they
    fall back to report-only),
  * RETIRE whose VAMSYS End Date is already set (must be skipped),
  * UPDATE without changes / with only an end_date change, needs_review UPDATE / CREATE,
  * UPDATE with empty target fields (must fall back to current, Cost Index = current),
  * CREATE depending on an AIRPORT_ADD airport.
``previous_world()`` is the same world one run earlier with known differences for diff_runs.
"""

from __future__ import annotations

import copy
from pathlib import Path

from vcca.models import (Decision, DecisionCategory as C, EvidenceSummary, FieldChange, Reason, ReasonCode as RC,
                         RouteTarget, RunMeta, VRoute)
from vcca.util_time import parse_hhmm
from vcca.vamsys import VAirport, VamsysSnapshot, VFleet

EFFECTIVE = "2026-11-26 16:00:00"
FLEETS = ("26071", "26072", "26722", "26718")
AIRPORTS = ("ZBAA", "ZSSS", "ZGGG", "ZSPD", "EBLG", "ZBAD", "ZUUU")


def vroute(rid, callsign, orig, dest, dep, arr, days, *, fleets=("26072",), end_date="", cost_index="80",
           type_=None) -> VRoute:
    return VRoute(id=str(rid), op=callsign[:3], callsign=callsign, number="CA" + callsign[3:], orig=orig, dest=dest,
                  type=type_ or ("cargo" if callsign.startswith("CAO") else "scheduled"), dep=dep, arr=arr,
                  dep_min=parse_hhmm(dep), arr_min=parse_hhmm(arr), days=frozenset(days),
                  fleet_ids=frozenset(fleets), start_date="2026-05-10 22:43:42", end_date=end_date,
                  cost_index=cost_index, hidden=False, tags="", is_special=False)


ROUTES = [
    vroute("100", "CCA1001", "ZBAA", "ZSSS", "01:00", "03:10", range(7)),                     # UPDATE
    vroute("101", "CCA1002", "ZSSS", "ZBAA", "05:00", "07:10", {0, 2, 4}, cost_index=""),     # UPDATE (fallbacks)
    vroute("102", "CCA1003", "ZBAA", "ZGGG", "02:00", "05:10", range(7)),                     # KEEP
    vroute("103", "CCA1004", "ZGGG", "ZBAA", "08:00", "11:00", {1}),                          # RETIRE
    vroute("104", "CCA1005", "ZBAA", "ZUUU", "09:00", "12:00", {2}),                          # RETIRE_REDUNDANT
    vroute("105", "CAO1006", "ZSPD", "EBLG", "22:15", "09:49", {0}, fleets=("26722",), cost_index="150"),  # DEFERRED
    vroute("106", "CCA1007", "ZBAA", "ZSSS", "10:00", "12:10", {3}, end_date="2026-11-20 16:00:00"),  # retiring
    vroute("107", "CCA1008", "ZSSS", "ZGGG", "11:00", "13:10", {4}),                          # HOLD
    vroute("108", "BJN8001", "ZBAD", "ZSSS", "12:00", "14:00", {5}, fleets=("26718",)),       # KEEP_UNPLANNED
    vroute("109", "CCA1009", "ZBAA", "ZGGG", "13:00", "16:00", {6}),                          # UPDATE review
    vroute("110", "CCA1010", "ZGGG", "ZBAA", "14:00", "17:00", {6}),                          # UPDATE no changes
    vroute("111", "CCA1011", "ZBAA", "ZUUU", "15:00", "18:00", {6}, end_date="2026-11-01 16:00:00"),  # end_date only
]


def snapshot(routes=ROUTES) -> VamsysSnapshot:
    aps = {c: VAirport(code=c, icao=c, iata="", name=c) for c in AIRPORTS}
    fleets = {f: VFleet(id=f, name=f, type_code={"26071": "A319", "26072": "A320", "26722": "B77L",
                                                 "26718": "B738"}[f],
                        kind="cargo" if f == "26722" else "pax") for f in FLEETS}
    return VamsysSnapshot(workspace_id="t", workspace_name="t", revision=7, storage_revision=7, db_path=Path("t.db"),
                          airports=aps, fleets=fleets, aircraft=[], routes=list(routes))


def _t(vr: VRoute, **kw) -> RouteTarget:
    t = RouteTarget.from_vroute(vr)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _ev(state="active", acts=10) -> EvidenceSummary:
    return EvidenceSummary(state=state, acts=acts, obs=acts + 2, recent_acts=acts // 2, last_act="2026-11-24",
                           banks=[{"dep": "01:00", "arr": "03:10", "fd3": "1234567", "acts": acts, "obs": acts + 2,
                                   "recent_obs": 3}], ac={"A320": acts})


def _vd(cat, vr: VRoute, changes=(), reasons=(), review=False, **tkw) -> Decision:
    cur = RouteTarget.from_vroute(vr)
    tgt = _t(vr, **tkw)
    d = Decision(category=cat, target=tgt, vroute_id=vr.id, current=cur, changes=list(changes),
                 reasons=list(reasons), evidence=_ev(), group_key=vr.key, plan_refs=["plan.xls:12"], slot_index="1/1")
    if review:
        d.add_reason(RC.WEEKDAY_DROPPED, review=True, weeks=4, old=[0, 2, 4], new=[0, 2])
    return d


def _retire(cat, vr: VRoute, code=RC.RETIRE_NEVER_FLOWN) -> Decision:
    has_end = bool(vr.end_date)
    d = _vd(cat, vr, changes=[] if has_end else [FieldChange("end_date", "", EFFECTIVE)],
            end_date=vr.end_date if has_end else EFFECTIVE)
    d.reasons = [Reason(RC.ALREADY_RETIRING, {"end_date": vr.end_date})] if has_end else [Reason(code, {"days": 365})]
    return d


def _new(cat, callsign, orig, dest, dep, arr, days, *, fleets=("26071", "26072"), ci=80, review=False,
         dep_airports=(), group=True, slot="1/1") -> Decision:
    t = RouteTarget(op=callsign[:3], callsign=callsign, number="CA" + callsign[3:], orig=orig, dest=dest,
                    type="cargo" if callsign.startswith("CAO") else "scheduled", dep=dep, arr=arr,
                    days=frozenset(days), fleet_ids=tuple(fleets), cost_index=ci, start_date=EFFECTIVE)
    d = Decision(category=cat, target=t, reasons=[Reason(RC.PLAN_FR24_CONFIRMED, {"acts": 9})], evidence=_ev(),
                 group_key=t.key if group else None, plan_refs=["plan.xls:20"] if group else [], slot_index=slot,
                 depends_on_airports=list(dep_airports),
                 details={"plan_dep": dep, "plan_arr": arr, "plan_days": sorted(days)} if group else {})
    if review:
        d.add_reason(RC.UNRATED_AIRPORT, review=True, airports=[orig])
    return d


def decisions() -> list[Decision]:
    r = {vr.id: vr for vr in ROUTES}
    ds = [
        Decision(category=C.AIRPORT_ADD, target=RouteTarget(op="", callsign="", number="", orig="ZBAL", dest=""),
                 reasons=[Reason(RC.AIRPORT_MISSING, {"airports": ["ZBAL"]})], details={"name": "BAYANHOT"}),
        _vd(C.UPDATE, r["100"], changes=[FieldChange("dep", "01:00", "01:20"), FieldChange("arr", "03:10", "03:30")],
            reasons=[Reason(RC.TIME_CALIBRATED, {"plan_dep": "01:00", "plan_arr": "03:10", "diff": 20})],
            dep="01:20", arr="03:30"),
        # empty target fleets / cost index -> CSV falls back to current (cost index stays blank as in VAMSYS)
        _vd(C.UPDATE, r["101"], changes=[FieldChange("days", [0, 2, 4], [0, 2, 4, 6])],
            days=frozenset({0, 2, 4, 6}), fleet_ids=(), cost_index=None),
        _vd(C.KEEP, r["102"], reasons=[Reason(RC.PLAN_FR24_CONFIRMED, {"acts": 30})]),
        _retire(C.RETIRE, r["103"]),
        _retire(C.RETIRE_REDUNDANT, r["104"], RC.VAMSYS_REDUNDANT),
        _retire(C.RETIRE_DEFERRED, r["105"], RC.DEFERRED_PLAN_NOT_FLOWN),
        _retire(C.RETIRE, r["106"]),
        _vd(C.HOLD, r["107"], reasons=[Reason(RC.HOLD_ROTATION_GAP_BLIND, {})]),
        _vd(C.KEEP_UNPLANNED, r["108"], reasons=[Reason(RC.UNPLANNED_ACTIVE, {"state": "active"})]),
        _vd(C.UPDATE, r["109"], changes=[FieldChange("days", [6], [5])], review=True, days=frozenset({5})),
        _vd(C.UPDATE, r["110"]),
        _vd(C.UPDATE, r["111"], changes=[FieldChange("end_date", "2026-11-01 16:00:00", "")],
            reasons=[Reason(RC.END_DATE_CLEARED, {"end_date": "2026-11-01 16:00:00"})], end_date=""),
        _new(C.CREATE_PLAN, "CCA2001", "ZBAA", "ZBAL", "06:00", "08:00", {0, 3}, dep_airports=["ZBAL"]),
        _new(C.CREATE_EXTRA_SLOT, "CCA2002", "ZBAA", "ZSSS", "20:00", "22:10", {1}, slot="2/2"),
        _new(C.CREATE_FR24, "CAO2003", "ZSPD", "EBLG", "23:00", "10:00", {2}, fleets=("26722",), ci=None,
             group=False),
        _new(C.CREATE_PLAN, "CCA2004", "ZGGG", "ZUUU", "07:00", "09:30", {4}, review=True),
        _new(C.WATCH_PLAN, "CCA2005", "ZBAA", "ZUUU", "07:00", "10:00", {4}),
        _new(C.CHARTER, "CCA2006", "ZBAA", "ZGGG", "03:00", "06:00", {5}, group=False),
        _new(C.POSITIONING, "CCA200A", "ZBAA", "ZSSS", "04:00", "06:00", {5}, group=False),
        _new(C.CREATE_FR24, "CCA2007", "ZSSS", "ZUUU", "05:00", "08:00", {1}, group=False),
    ]
    ds[-1].add_reason(RC.ROTATION_GAP_LEG, out_callsign="CCA2007", ret_callsign="CCA2008")
    return ds


def meta(ds: list[Decision]) -> RunMeta:
    from collections import Counter

    return RunMeta(season="test", season_label="测试航季", generated_at="2026-11-26 05:27:01",
                   effective_time=EFFECTIVE, season_start="2026-10-25", season_end="2027-03-27", plan_rows=10,
                   plan_legs=12, plan_numbers=8, vamsys_revision=7, vamsys_storage_revision=7,
                   vamsys_routes=len(ROUTES), vamsys_commercial_routes=len(ROUTES), fr24_rows=100,
                   fr24_latest_ts=1795662300, fr24_last_fetch="2026-11-26 05:16:27",
                   thresholds={"recent_window_days": 21, "bank_merge": [[45, 60], [20, 75]]},
                   threshold_hits={"time_override_min_acts": 3, "extra_only_hit": 1},
                   counts=dict(Counter(str(d.category) for d in ds)))


def world():
    ds = decisions()
    return meta(ds), ds, snapshot()


def previous_world():
    """Earlier run: CCA1001 not yet changed (UPDATE->KEEP), CCA2002 extra slot absent, an extra CHARTER that
    disappeared since, CCA1003 KEEP identical."""
    ds = [copy.deepcopy(d) for d in decisions()]
    out = []
    for d in ds:
        if d.target.callsign == "CCA2002":
            continue  # -> 'added' now
        if d.vroute_id == "100":
            d.category = C.KEEP
            d.changes = []
            d.target.dep, d.target.arr = "01:00", "03:10"
        out.append(d)
    gone = _new(C.CHARTER, "CCA2999", "ZBAA", "ZSSS", "03:00", "05:00", {6}, group=False)
    out.append(gone)
    return meta(out), out
