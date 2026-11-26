"""CAAC season pre-plan (.xls) -> PlanLeg / PlanGroup / RotationGap (plan 5.1).

Port of ``load_raw_preplan`` / ``detect_rotation_gaps`` from the former script rebuild_vamsys_plan.py (removed; see
git history); leg output was verified field-for-field identical before the script was removed.

Row layout (CAAC 三字码 files, airport codes are actually ICAO):
  FD1 callsign  FD2 aircraft  FD3 weekday mask ('1.3.5.7')
  FD4 orig  FD5 dep  FD6 arr  FD7 stop/dest  FD8 dep2  FD9 arr2  FD10 dest2
  FD19 alt-aircraft boilerplate  FD23 from MMDD  FD24 to MMDD  FD26 service type ('F' cargo)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

from .biff8 import read_biff8_sheets
from .models import PlanData, PlanGroup, PlanLeg, PlanSlot, RotationGap, RouteKey
from .util_time import fd3_to_days, fmt_hm, local_to_utc, parse_hhmm, shift_days

if TYPE_CHECKING:
    from .config import Config


def _s(d: dict, k: str) -> str:
    return str(d.get(k) or "").strip()


def iter_plan_rows(path: Path) -> Iterable[tuple[str, int, dict]]:
    """Yield (sheet_name, row_no, {header: value}) for every non-empty data row (row_no: header row = 1)."""
    sheets = read_biff8_sheets(path)
    for sname, tbl in sheets.items():
        if not tbl:
            continue
        hdr = tbl[0]
        for r_i, row in enumerate(tbl[1:], 2):
            if not row or not row[0]:
                continue
            yield sname, r_i, {hdr[i]: (row[i] if i < len(row) else "") for i in range(len(hdr))}


def parse_row_legs(d: dict, *, target_ops: Sequence[str], number_prefix: str = "CA", tz_offset_min: int = 480,
                   cargo_ops: Sequence[str] = ("CAO",), src_file: str = "", src_row: int = 0,
                   plan_group: str = "") -> list[PlanLeg]:
    """One CAAC row -> 0..2 PlanLegs. Returns [] for non-target callsigns.

    Leg 1: FD4->FD7 at FD5/FD6. Leg 2: FD7->FD10 at FD8/FD9 with cumulative day offset
    (+1 if leg 1 arrives after midnight, +1 more if leg 2 departs earlier than leg 1 arrives).
    UTC = local - tz; weekday set shifted by the resulting day shift (e.g. BJT dep < 08:00 -> previous day).
    """
    cs = _s(d, "FD1").upper()
    if not cs.startswith(tuple(target_ops)):
        return []
    op = cs[:3]
    num = number_prefix + cs[3:]
    fd2 = _s(d, "FD2").upper()
    fd19 = _s(d, "FD19")
    fd23 = _s(d, "FD23").zfill(4)
    fd24 = _s(d, "FD24").zfill(4)
    fd26 = _s(d, "FD26").upper()
    fd3 = str(d.get("FD3") or "")
    bjt_days = fd3_to_days(fd3)
    single_day = fd23 == fd24
    route_type = "cargo" if (op in cargo_ops or fd26 == "F") else "scheduled"
    common = dict(op=op, callsign=cs, number=num, fd2=fd2, fd19=fd19, fd26=fd26, fd23=fd23, fd24=fd24,
                  single_day=single_day, route_type=route_type, plan_group=plan_group, src_file=src_file,
                  src_row=src_row, bjt_days=fd3)
    legs: list[PlanLeg] = []

    o1 = _s(d, "FD4").upper()
    d1 = _s(d, "FD7").upper()
    m_dep1 = parse_hhmm(d.get("FD5"))
    m_arr1 = parse_hhmm(d.get("FD6"))
    day_off = 0
    if o1 and d1 and m_dep1 is not None and m_arr1 is not None:
        utc_d1, shift1 = local_to_utc(m_dep1, tz_offset_min, 0)
        utc_a1 = (m_arr1 - tz_offset_min) % 1440
        legs.append(PlanLeg(
            orig=o1, dest=d1, leg_no=1, is_multileg=bool(d.get("FD10")), day_offset=0,
            bjt_dep=fmt_hm(m_dep1), bjt_arr=fmt_hm(m_arr1), utc_dep=fmt_hm(utc_d1), utc_arr=fmt_hm(utc_a1),
            utc_dep_min=utc_d1, utc_arr_min=utc_a1, utc_days=shift_days(bjt_days, shift1),
            src_ref=f"{src_file}:{src_row}", **common,
        ))
        if m_arr1 < m_dep1:
            day_off += 1

    d2_ap = _s(d, "FD10").upper()
    if d1 and d2_ap:
        m_dep2 = parse_hhmm(d.get("FD8"))
        m_arr2 = parse_hhmm(d.get("FD9"))
        if m_dep2 is not None and m_arr2 is not None:
            if m_arr1 is not None and m_dep2 < m_arr1:
                day_off += 1
            utc_d2, shift2 = local_to_utc(m_dep2, tz_offset_min, day_off)
            utc_a2 = (m_arr2 - tz_offset_min) % 1440
            legs.append(PlanLeg(
                orig=d1, dest=d2_ap, leg_no=2, is_multileg=True, day_offset=day_off,
                bjt_dep=fmt_hm(m_dep2), bjt_arr=fmt_hm(m_arr2), utc_dep=fmt_hm(utc_d2), utc_arr=fmt_hm(utc_a2),
                utc_dep_min=utc_d2, utc_arr_min=utc_a2, utc_days=shift_days(bjt_days, shift2),
                src_ref=f"{src_file}:{src_row}#2", **common,
            ))
    return legs


def load_plan(cfg: "Config") -> PlanData:
    """Read every ``cfg.plan_files`` -> PlanData (target-operator raw rows + legs, file order preserved)."""
    rows: list[dict] = []
    legs: list[PlanLeg] = []
    files: dict[str, int] = {}
    for path, grp in zip(cfg.plan_files, cfg.plan_groups):
        n = 0
        for _sheet, r_i, d in iter_plan_rows(path):
            cs = _s(d, "FD1").upper()
            if not cs.startswith(cfg.target_ops):
                continue
            n += 1
            rows.append(d)
            legs.extend(parse_row_legs(
                d, target_ops=cfg.target_ops, number_prefix=cfg.number_prefix, tz_offset_min=cfg.plan_tz_offset_min,
                cargo_ops=cfg.cargo_ops, src_file=path.name, src_row=r_i, plan_group=grp,
            ))
        files[path.name] = n
    return PlanData(rows=rows, legs=legs, files=files,
                    fd23_mode=_mode(_s(r, "FD23").zfill(4) for r in rows),
                    fd24_mode=_mode(_s(r, "FD24").zfill(4) for r in rows))


def fd23_fd24_modes(paths: Iterable[Path], target_ops: Sequence[str]) -> tuple[str, str]:
    """Most common FD23 / FD24 ('MMDD') over target-operator rows (season start/end default)."""
    f23, f24 = Counter(), Counter()
    for p in paths:
        for _s_, _r, d in iter_plan_rows(Path(p)):
            if _s(d, "FD1").upper().startswith(tuple(target_ops)):
                f23[_s(d, "FD23").zfill(4)] += 1
                f24[_s(d, "FD24").zfill(4)] += 1
    return (f23.most_common(1)[0][0] if f23 else "", f24.most_common(1)[0][0] if f24 else "")


def _mode(vals: Iterable[str]) -> str:
    c = Counter(v for v in vals if v.strip("0"))
    return c.most_common(1)[0][0] if c else ""


# ------------------------------------------------------------------ grouping -----------------------------------------
def build_slots(legs: Sequence[PlanLeg]) -> list[PlanSlot]:
    """Merge legs with identical UTC (dep, arr) into slots (days = union), sorted by (dep, arr).
    Raw plan only: no FR24 calibration and no FD5-as-UTC correction (that is reconcile's job)."""
    days: dict[tuple[str, str], set[int]] = defaultdict(set)
    ref: dict[tuple[str, str], PlanLeg] = {}
    refs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for l in legs:
        k = (l.utc_dep, l.utc_arr)
        days[k] |= l.utc_days
        ref[k] = l
        refs[k].append(l.src_ref)
    out = []
    for k in sorted(days):
        l = ref[k]
        out.append(PlanSlot(dep=k[0], arr=k[1], days=frozenset(days[k]),
                            bjt=f"{l.bjt_days_label} {l.bjt_dep}-{l.bjt_arr}", fd2=l.fd2, fd19=l.fd19,
                            src_refs=tuple(refs[k]), single_day=all(x.single_day for x in legs if (x.utc_dep, x.utc_arr) == k)))
    return out


def group_legs(legs: Iterable[PlanLeg], alias: dict[str, str] | None = None) -> dict[RouteKey, PlanGroup]:
    """Group legs by (op, callsign, orig, dest) with airports mapped through ``alias`` (VAMSYS codes).
    raw_slots come from regular legs, or from single-day legs when the key has no regular legs."""
    alias = alias or {}
    by_key: dict[RouteKey, list[PlanLeg]] = defaultdict(list)
    for l in legs:
        by_key[(l.op, l.callsign, alias.get(l.orig, l.orig), alias.get(l.dest, l.dest))].append(l)
    out = {}
    for key in sorted(by_key):
        ls = by_key[key]
        regular = [l for l in ls if not l.single_day]
        first = (regular or ls)[0]
        out[key] = PlanGroup(key=key, legs=ls, raw_slots=build_slots(regular or ls), route_type=first.route_type,
                             plan_group=first.plan_group, raw_orig=first.orig, raw_dest=first.dest)
    return out


def detect_rotation_gaps(legs: Iterable[PlanLeg], number_prefix: str = "CA") -> dict[RouteKey, RotationGap]:
    """Odd number N ends at X, N+1 starts at Y, X != Y -> missing foreign leg X->Y under N's callsign
    (e.g. CAO3155 ESSA->EBLG). Single-day legs are ignored. Key = (op, out_callsign, X, Y), plan codes."""
    by_num: dict[str, list[PlanLeg]] = defaultdict(list)
    for l in legs:
        if not l.single_day:
            by_num[l.number].append(l)
    gaps: dict[RouteKey, RotationGap] = {}
    pat = re.compile(re.escape(number_prefix) + r"(\d+)")
    for num, out_legs in sorted(by_num.items()):
        m = pat.fullmatch(num)
        if not m or int(m.group(1)) % 2 == 0:
            continue
        ret_legs = by_num.get(f"{number_prefix}{int(m.group(1)) + 1}")
        if not ret_legs:
            continue
        out_end = {l.dest for l in out_legs} - {l.orig for l in out_legs}
        ret_start = {l.orig for l in ret_legs} - {l.dest for l in ret_legs}
        if len(out_end) != 1 or len(ret_start) != 1 or out_end == ret_start:
            continue
        x, y = next(iter(out_end)), next(iter(ret_start))
        o = out_legs[0]
        g = RotationGap(op=o.op, out_callsign=o.callsign, ret_callsign=ret_legs[0].callsign, orig=x, dest=y,
                        route_type=o.route_type)
        gaps[g.key] = g
    return gaps


# ------------------------------------------------------------------ FD5-as-UTC support -------------------------------
def fd5_as_utc_alternative(leg: PlanLeg) -> tuple[str, frozenset[int]]:
    """If FD5 was (wrongly) written in UTC, the UTC departure is the BJT field verbatim and the weekdays are the
    unshifted FD3 set (legacy detect_mixed_tz_and_choose_utc). Returns (dep_hhmm, days). The *detection*
    (compare with FR24 banks) lives in evidence.fd5_utc_check; this only supplies the alternative reading."""
    return leg.bjt_dep, fd3_to_days(leg.bjt_days)


def bjt_block_min(leg: PlanLeg) -> int:
    """Block time from the plan's local times (minutes, 0..1439)."""
    return (parse_hhmm(leg.bjt_arr) - parse_hhmm(leg.bjt_dep)) % 1440


def legacy_leg_dict(leg: PlanLeg) -> dict:
    """PlanLeg -> legacy load_raw_preplan() dict (for parity tests / old report code)."""
    return {
        "op": leg.op, "cs": leg.callsign, "num": leg.number, "orig": leg.orig, "dest": leg.dest,
        "leg_no": leg.leg_no, "is_multileg": leg.is_multileg, "bjt_days": leg.bjt_days_label,
        "bjt_dep": leg.bjt_dep, "bjt_arr": leg.bjt_arr, "utc_dep": leg.utc_dep, "utc_arr": leg.utc_arr,
        "utc_dep_min": leg.utc_dep_min, "utc_arr_min": leg.utc_arr_min, "utc_days": set(leg.utc_days),
        "fd2": leg.fd2, "fd19": leg.fd19, "fd26": leg.fd26, "fd23": leg.fd23, "fd24": leg.fd24,
        "expired": leg.single_day, "grp": leg.plan_group, "route_type": leg.route_type, "src_ref": leg.src_ref,
    }
