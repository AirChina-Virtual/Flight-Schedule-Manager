"""Plan x FR24 x VAMSYS reconciliation -> decisions (plan 5.5 / 5.6 / 5.8; step 5).

Ports the decision part of the former ``rebuild_vamsys_plan.main`` (script removed; see git history) and ``calibrate_slots_with_fr24``.
Inputs are fully typed (no string-matching on notes). Output: ``BuildResult`` -> decisions.json.

Flow of ``build`` (CLI ``vcca_plan.py build``):
  cfg -> caac.load_plan -> caac.group_legs(alias) + caac.detect_rotation_gaps
      -> vamsys.load_vamsys
      -> fr24_store (readonly) rows -> fr24_obs.build_observations -> evidence.build_all_evidence
      -> reconcile(...) -> BuildResult
      -> check_closure (abort on error) -> models.dump_decisions(run_dir/'decisions.json')
      -> store.upsert_targets(result.target_states)   (refresh tiers for the next fetch, plan 6.2)

Decision table (5.8):
  A. plan group x VAMSYS (key op,cs,orig,dest; on carrier change also the original-carrier key):
     no VAMSYS + ACTIVE -> CREATE_PLAN per calibrated slot; no VAMSYS + not ACTIVE -> WATCH_PLAN;
     VAMSYS + ACTIVE -> greedy pairing (|dep diff| + slot_pair_day_penalty x |days symmetric diff|):
        paired & differs -> UPDATE; paired & same -> KEEP;
        unpaired VAMSYS id within vamsys_bank_match of a bank -> UPDATE else RETIRE_REDUNDANT;
        unpaired slot -> CREATE_EXTRA_SLOT;
     VAMSYS + not ACTIVE -> RETIRE_DEFERRED.
     (SCHED_ONLY with current schedules counts as confirmed for an EXISTING VAMSYS route — as legacy
      ``direct_sched`` — so FR24 future schedules never defer a planned route; it never creates.)
  B. VAMSYS only: ACTIVE/SCHED_ONLY (not occasional old-carrier) -> KEEP_UNPLANNED calibrated => UPDATE/KEEP;
     rotation-gap leg without FR24 actuals -> HOLD; NO_DATA -> HOLD (refetch, review); else RETIRE with the
     specific reason.
  C. FR24 only: acts >= fr24_add_min_acts over >= fr24_add_min_weeks (rotation gap: rotation_gap_add_min_acts)
     -> CREATE_FR24 (one row per bank, each weekday in one bank only); irregular -> CHARTER;
     bad callsign / orig==dest -> POSITIONING.
  D. missing airports -> AIRPORT_ADD (+ depends_on_airports on the routes).
  E. idempotency: End Date already set -> no second retire (decision kept, ``changes`` empty, ALREADY_RETIRING);
     still needed -> UPDATE clearing end_date (review); UPDATE keeps Cost Index, creates use
     cfg.default_cost_index[op]; every commercial VAMSYS route gets exactly one decision in VROUTE_CATEGORIES
     (``check_closure``). Routes with an End Date are paired only after routes without one.
Every threshold application increments ``BuildResult.threshold_hits[<threshold name>]`` (plan 5.9 report).

Export contract: RETIRE* decisions carry ``changes=[end_date '' -> effective_time]``; already-retiring ones have
``changes == []`` and must not be written to the retire CSVs (same rule as UPDATE: only rows with changes).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Sequence

from . import util_time as ut
from .evidence import build_evidence, fd5_utc_check
from .fleets import FleetRecommendation, audit_fleets, recommend_fleets
from .models import (VROUTE_CATEGORIES, Bank, Confidence, Decision, DecisionCategory, Evidence, EvidenceState,
                     EvidenceSummary, FieldChange, PlanData, PlanGroup, PlanSlot, Priority, Reason, ReasonCode,
                     RotationGap, RouteKey, RouteTarget, RunMeta, TargetKind, TargetState, VRoute)

if TYPE_CHECKING:
    from .config import Config
    from .fr24_obs import ObsIndex
    from .vamsys import VamsysSnapshot

LogFn = Callable[[str], None]
C = DecisionCategory
RC = ReasonCode
S = EvidenceState

CREATE_CATS = frozenset({C.CREATE_PLAN, C.CREATE_EXTRA_SLOT, C.CREATE_FR24})
RETIRE_CATS = frozenset({C.RETIRE, C.RETIRE_REDUNDANT, C.RETIRE_DEFERRED})
REPORT_CATS = frozenset({C.WATCH_PLAN, C.CHARTER, C.POSITIONING})
COMPARED_FIELDS = ("callsign", "number", "type", "dep", "arr", "days", "fleet_ids", "end_date")


@dataclass(slots=True)
class CalibratedSlot:
    """A plan slot after FR24 calibration (5.5) — the unit that becomes one VAMSYS route."""
    dep: str
    arr: str
    days: frozenset[int]
    plan_dep: str = ""             # '' for FR24-only extra slots
    plan_arr: str = ""
    plan_days: frozenset[int] = frozenset()
    bank_index: int | None = None  # index into Evidence.banks
    from_fr24_extra: bool = False
    subslots: list[PlanSlot] = field(default_factory=list)
    reasons: list = field(default_factory=list)          # list[models.Reason]
    needs_review: bool = False
    review_flags: list = field(default_factory=list)
    unflown: bool = False          # plan slot with no FR24 bank although the key was observed >= drop weeks

    @property
    def src_refs(self) -> list[str]:
        return [r for s in self.subslots for r in s.src_refs]

    def add(self, code: str, review: bool = False, **params) -> None:
        self.reasons.append(Reason(str(code), params))
        if review:
            self.needs_review = True
            if str(code) not in self.review_flags:
                self.review_flags.append(str(code))


@dataclass(slots=True)
class BuildResult:
    decisions: list[Decision]
    meta: RunMeta
    target_states: list[TargetState] = field(default_factory=list)
    threshold_hits: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)


# =====================================================================================================================
# small helpers
# =====================================================================================================================
def _d(a: str, b: str) -> int:
    return abs(ut.hm_circ_diff(a, b))


def _within(pair: tuple[int, int], dep1: str, arr1: str, dep2: str, arr2: str) -> bool:
    if not (dep1 and arr1 and dep2 and arr2):
        return False
    return _d(dep1, dep2) <= pair[0] and _d(arr1, arr2) <= pair[1]


def _same_date(b1: Bank, b2: Bank, wd: int) -> bool:
    """Both banks have an actual departure on the same UTC date that falls on weekday ``wd``."""
    d1 = {ut.ts_utc_date(t) for t in b1.act_ts if ut.ts_utc_weekday(t) == wd}
    return bool(d1) and any(ut.ts_utc_date(t) in d1 for t in b2.act_ts)


#: default for the (not yet configurable) threshold ``weekday_drop_min_coverage`` (plan 5.5, weekday drop):
#: a weekday is dropped only when every remaining planned weekday was flown in >= this share of the observed weeks.
WEEKDAY_DROP_MIN_COVERAGE = 0.75


def _drop_coverage(th) -> float:
    return float(getattr(th, "weekday_drop_min_coverage", WEEKDAY_DROP_MIN_COVERAGE))


def _weekday_occurrences(wd: int, t0: int, t1: int) -> int:
    """Number of UTC dates with weekday ``wd`` between the dates of t0 and t1 (inclusive)."""
    d0, d1 = ut.ts_to_dt(t0).date(), ut.ts_to_dt(t1).date()
    n = (d1 - d0).days + 1
    if n <= 0:
        return 0
    first = (wd - d0.weekday()) % 7
    return 0 if first >= n else (n - 1 - first) // 7 + 1


def _coverage_ok(keep_days, weeks_by_wd: Mapping[int, int], t0: int | None, t1: int | None, th) -> bool:
    """Absence of a weekday is evidence only if the route is otherwise well observed: each kept weekday was flown
    on >= weekday_drop_min_coverage of its own occurrences between the first and last actual (partial weeks at
    the season start / data end do not count against it)."""
    if not keep_days or t0 is None or t1 is None:
        return False
    cov = _drop_coverage(th)
    for wd in keep_days:
        occ = _weekday_occurrences(wd, t0, t1)
        if occ <= 0 or weeks_by_wd.get(wd, 0) / occ < cov:
            return False
    return True


def _day_shift(from_hm: str, to_hm: str) -> int:
    """Weekday shift when a departure moves from ``from_hm`` to ``to_hm`` by the shortest way round the clock
    (23:55 -> 00:25 = +1, 00:10 -> 23:50 = -1, else 0)."""
    if not from_hm or not to_hm:
        return 0
    m1 = ut.parse_hhmm(from_hm)
    return (m1 + ut.circ_diff(ut.parse_hhmm(to_hm), m1)) // 1440


def _shift_wd(d: Mapping[int, Any], k: int) -> dict:
    return {(wd + k) % 7: v for wd, v in d.items()} if k else dict(d)


def _key_frame_stats(ev: Evidence, frame_dep: str, recent_start: int | None):
    """Key-level actual weekday stats of all (non-superseded) banks, expressed in the time frame of ``frame_dep``:
    ({wd: acts}, {wd: set(week idx)}, {wd: recent acts}, first_act_ts, last_act_ts)."""
    acts: dict[int, int] = defaultdict(int)
    weeks: dict[int, set] = defaultdict(set)
    recent: dict[int, int] = defaultdict(int)
    ts_all = []
    for b in ev.banks:
        k = _day_shift(b.mode_dep, frame_dep)
        for wd, n in b.day_acts.items():
            acts[(wd + k) % 7] += n
        for wd, ws in b.day_weeks.items():
            weeks[(wd + k) % 7] |= set(ws)
        for t in b.act_ts:
            ts_all.append(t)
            if recent_start is None or t >= recent_start:
                recent[(ut.ts_utc_weekday(t) + k) % 7] += 1
    return acts, weeks, recent, (min(ts_all) if ts_all else None), (max(ts_all) if ts_all else None)


def _bank_recent_on(b: Bank, wd: int | None, recent_start: int | None) -> bool:
    """Bank has an actual in the recent window (on weekday ``wd`` when given)."""
    if recent_start is None:
        return True
    return any(t >= recent_start and (wd is None or ut.ts_utc_weekday(t) == wd) for t in b.act_ts)


def _bank_act_weeks(b: Bank) -> int:
    return len(set().union(*b.day_weeks.values())) if b.day_weeks else 0


def _act_weeks(ev: Evidence, anchor: int) -> int:
    return len({ut.week_index(o.sched_dep or o.real_dep or o.ev_ts, anchor) for o in ev.observations if o.is_actual})


def _is_confirmed(ev: Evidence | None) -> bool:
    return ev is not None and ev.state in (S.ACTIVE, S.SCHED_ONLY)


def _plan_slots_with_fd5(cfg: "Config", legs, ev: Evidence | None) -> tuple[list[PlanSlot], dict[tuple, str]]:
    """Raw plan slots (identical UTC dep/arr merged) after the FD5-as-UTC check against FR24 banks."""
    days: dict[tuple[str, str], set[int]] = defaultdict(set)
    ref: dict[tuple[str, str], Any] = {}
    refs: dict[tuple[str, str], list[str]] = defaultdict(list)
    fd5: dict[tuple[str, str], str] = {}
    for l in legs:
        dep, arr, ds = l.utc_dep, l.utc_arr, l.utc_days
        if ev is not None and ev.banks:
            chk = fd5_utc_check(l, ev, tz_offset_min=cfg.plan_tz_offset_min)
            if chk.suspect:
                dep, arr, ds = chk.utc_dep, chk.utc_arr, chk.days
                fd5[(dep, arr)] = l.bjt_dep
        k = (dep, arr)
        days[k] |= ds
        ref[k] = l
        refs[k].append(l.src_ref)
    out = []
    for k in sorted(days):
        l = ref[k]
        out.append(PlanSlot(dep=k[0], arr=k[1], days=frozenset(days[k]),
                            bjt=f"{l.bjt_days_label} {l.bjt_dep}-{l.bjt_arr}", fd2=l.fd2, fd19=l.fd19,
                            src_refs=tuple(refs[k]), single_day=l.single_day))
    return out, fd5


# =====================================================================================================================
# 5.5 / 5.6 calibration
# =====================================================================================================================
def calibrate_slots(cfg: "Config", group: PlanGroup, evidence: Evidence | None,
                    vamsys_routes: Sequence[VRoute] = (), *, promoted: bool = False,
                    hits: Counter | None = None) -> list[CalibratedSlot]:
    """5.5 / 5.6: merge micro-splits, FR24 time override, weekday add/drop, missing banks, single-day promotion."""
    th = cfg.thresholds
    hits = hits if hits is not None else Counter()
    confirmed = _is_confirmed(evidence)
    ev = evidence if confirmed else None
    banks: list[Bank] = list(ev.banks) if ev else []
    legs = group.regular_legs or group.legs
    raw, fd5 = _plan_slots_with_fd5(cfg, legs, ev)

    # --- step 1: attach to the closest FR24 bank, merge micro-splits
    single_bank = confirmed and len(banks) == 1
    grouped: list[dict] = []
    for sl in raw:
        c_idx = None
        if banks:
            bi = min(range(len(banks)), key=lambda i: _d(sl.dep, banks[i].mode_dep) + _d(sl.arr, banks[i].mode_arr))
            if _within(th.plan_attach, sl.dep, sl.arr, banks[bi].mode_dep, banks[bi].mode_arr):
                c_idx = bi
        for g in grouped:
            same = c_idx is not None and g["c_idx"] == c_idx
            close = single_bank and _within(th.micro_split_merge, sl.dep, sl.arr, g["dep"], g["arr"])
            if same or close:
                g["days"] |= sl.days
                g["subslots"].append(sl)
                if close and not same:
                    g["close_merge"] = True
                if c_idx is not None and g["c_idx"] is None:
                    g["c_idx"] = c_idx
                break
        else:
            grouped.append({"dep": sl.dep, "arr": sl.arr, "days": set(sl.days), "plan_dep": sl.dep,
                            "plan_arr": sl.arr, "c_idx": c_idx, "subslots": [sl], "close_merge": False})

    # --- step 2a: bank of each grouped slot (attach, else calibrate within plan_calibrate); slots that end up on
    # the same FR24 bank are one operation (5.5 "同组内落入同一 FR24 时段" -> merge, weekdays = union)
    n_grouped = len(grouped)
    for g in grouped:
        g["bank"] = g["c_idx"]
        g["how"] = "attach" if g["c_idx"] is not None else ""
        if banks and g["bank"] is None:
            bi = min(range(len(banks)), key=lambda i: _d(g["dep"], banks[i].mode_dep) + _d(g["arr"], banks[i].mode_arr))
            cand = banks[bi]
            if (_within(th.plan_calibrate, g["dep"], g["arr"], cand.mode_dep, cand.mode_arr)
                    or (n_grouped == 1 and cand.acts >= th.time_override_min_acts)):
                g["bank"], g["how"] = bi, "calibrate"
    merged: list[dict] = []
    for g in grouped:
        tgt = next((m for m in merged if g["bank"] is not None and m["bank"] == g["bank"]), None)
        if tgt is None:
            merged.append(g)
        else:
            tgt["days"] |= g["days"]
            tgt["subslots"].extend(g["subslots"])
            tgt["how"] = tgt["how"] or g["how"]
    for g in merged:
        # representative plan time = the sub-slot carrying most weekdays (ties: earliest dep)
        rep = max(g["subslots"], key=lambda s: (len(s.days), -ut.parse_hhmm(s.dep)))
        g["dep"] = g["plan_dep"] = rep.dep
        g["arr"] = g["plan_arr"] = rep.arr

    total_act_days = frozenset().union(*(b.act_days for b in banks)) if banks else frozenset()
    used: set[int] = set()
    out: list[CalibratedSlot] = []
    for g in merged:
        cs = CalibratedSlot(dep=g["dep"], arr=g["arr"], days=frozenset(g["days"]), plan_dep=g["plan_dep"],
                            plan_arr=g["plan_arr"], plan_days=frozenset(g["days"]), subslots=g["subslots"])
        for sl in g["subslots"]:
            if (sl.dep, sl.arr) in fd5:
                cs.add(RC.FD5_UTC_SUSPECT, fd5=fd5[(sl.dep, sl.arr)])
        cl = None
        if g["bank"] is not None:
            cs.bank_index = g["bank"]
            hits["plan_attach" if g["how"] == "attach" else "plan_calibrate"] += 1
            cl = banks[cs.bank_index]
            used.add(cs.bank_index)
        if len(g["subslots"]) > 1:
            cs.add(RC.MICRO_SPLIT_MERGED, n=len(g["subslots"]))
            if g["close_merge"]:
                hits["micro_split_merge"] += 1
        if cl is not None:
            if cl.superseded:
                cs.add(RC.BANK_SUPERSEDED, old=[f"{x['mode_dep']}-{x['mode_arr']}({ut.ts_utc_date(x['last_ts'])})"
                                                for x in cl.superseded])
                hits["bank_supersede_days"] += 1
            dd, da = _d(cs.plan_dep, cl.mode_dep), _d(cs.plan_arr, cl.mode_arr)
            dst = cl.recent_obs >= th.time_override_min_acts and dd in th.dst_shift_min
            trust = cl.acts >= th.time_override_min_acts or dst
            if trust and (dd > th.time_tolerance_min or da > th.time_tolerance_min):
                k = _day_shift(cs.dep, cl.mode_dep)
                if k:
                    cs.days = ut.shift_days(cs.days, k)
                cs.dep, cs.arr = cl.mode_dep, cl.mode_arr
                if cl.acts < th.time_override_min_acts:
                    cs.add(RC.DST_SHIFT, diff=dd)
                    hits["dst_shift_min"] += 1
                else:
                    cs.add(RC.TIME_CALIBRATED, plan_dep=cs.plan_dep, plan_arr=cs.plan_arr, diff=max(dd, da))
                    hits["time_override_min_acts"] += 1
        # weekdays (plan is the base)
        if promoted:
            nd = (ut.shift_days(cl.act_days, _day_shift(cl.mode_dep, cs.dep))
                  if cl is not None and cl.act_days else total_act_days)
            if nd and nd != cs.days:
                cs.add(RC.WEEKDAYS_FROM_FR24, old=sorted(cs.days), new=sorted(nd))
                cs.days = frozenset(nd)
        elif cl is not None and cl.acts:
            rs = ev.info.get("recent_start_ts") if ev else None
            k2 = _day_shift(cl.mode_dep, cs.dep)            # bank frame -> slot frame
            dweeks = _shift_wd(cl.day_weeks, k2)
            dacts = _shift_wd(cl.day_acts, k2)
            add = {wd for wd, ws in dweeks.items()
                   if len(ws) >= th.weekday_add_min_weeks and _bank_recent_on(cl, (wd - k2) % 7, rs)} - cs.days
            blocked = {wd for wd, ws in dweeks.items() if len(ws) >= th.weekday_add_min_weeks} - cs.days - add
            if blocked:
                hits["weekday_add_min_weeks(not recent)"] += 1
            if add:
                new = cs.days | add
                cs.add(RC.WEEKDAY_ADDED, old=sorted(cs.days), new=sorted(new))
                cs.days = frozenset(new)
                hits["weekday_add_min_weeks"] += 1
            weeks = _bank_act_weeks(cl)
            drop = {wd for wd in cs.days if not dacts.get(wd)}     # 0 ACTUAL flights in this bank
            if drop and weeks >= th.weekday_drop_min_weeks and cs.days - drop:
                if _coverage_ok(cs.days - drop, {wd: len(ws) for wd, ws in dweeks.items()},
                                cl.act_ts[0] if cl.act_ts else None, cl.act_ts[-1] if cl.act_ts else None, th):
                    new = cs.days - drop
                    cs.add(RC.WEEKDAY_DROPPED, review=True, weeks=weeks, old=sorted(cs.days), new=sorted(new))
                    cs.days = frozenset(new)
                    hits["weekday_drop_min_weeks"] += 1
                else:
                    hits["weekday_drop_min_coverage(kept)"] += 1
        out.append(cs)

    # plan slots FR24 never shows (no bank within plan_calibrate although the key was observed for weeks)
    if confirmed and ev is not None and ev.season_weeks_observed >= th.weekday_drop_min_weeks:
        for cs in out:
            if cs.bank_index is None:
                cs.unflown = True

    # --- step 3: FR24 banks missing from the plan. Each weekday belongs to one bank (delay drift of the same
    # flight), unless both banks flew on the same date (a genuine second operation that day).
    if banks:
        live_v = [v for v in vamsys_routes if v.dep and v.arr and not v.has_end_date]
        slot_banks = [(s, banks[s.bank_index]) for s in out if s.bank_index is not None]
        for idx in sorted(range(len(banks)), key=lambda i: (-banks[i].acts, -banks[i].obs, i)):
            cl = banks[idx]
            if idx in used:
                continue
            if any(_within(th.extra_slot_separation, s.dep, s.arr, cl.mode_dep, cl.mode_arr) for s in out):
                continue
            v_match = any(_within(th.vamsys_bank_match, v.dep, v.arr, cl.mode_dep, cl.mode_arr) for v in live_v)
            if cl.acts >= th.extra_slot_min_acts or (v_match and cl.obs >= 1):
                days0 = cl.act_days or cl.days
                days = set()
                for wd in days0:
                    covering = [b for s, b in slot_banks if wd in s.days]
                    if not covering or any(_same_date(cl, b, wd) for b in covering):
                        days.add(wd)
                if not days:
                    hits["extra_slot_weekday_dedup"] += 1
                    continue
                cs = CalibratedSlot(dep=cl.mode_dep, arr=cl.mode_arr, days=frozenset(days), bank_index=idx,
                                    from_fr24_extra=True)
                slot_banks.append((cs, cl))
                cs.add(RC.EXTRA_SLOT, acts=cl.acts, obs=cl.obs)
                if cl.acts >= th.extra_slot_min_acts:
                    hits["extra_slot_min_acts"] += 1
                else:
                    cs.add(RC.VAMSYS_MATCHES_BANK, bank=cl.label)
                    hits["vamsys_bank_match"] += 1
                out.append(cs)
                used.add(idx)
    return out


def pair_slots(cfg: "Config", vroutes: Sequence[VRoute], slots: Sequence[CalibratedSlot]
               ) -> tuple[list[tuple[VRoute, CalibratedSlot]], list[VRoute], list[CalibratedSlot]]:
    """Greedy pairing (score = |dep diff| (300 when VAMSYS has no time) + slot_pair_day_penalty x |days ^|).
    Returns (pairs, unpaired_vroutes, unpaired_slots)."""
    pen = cfg.thresholds.slot_pair_day_penalty
    scored = []
    for vi, v in enumerate(vroutes):
        for si, s in enumerate(slots):
            sc = abs(ut.circ_diff(v.dep_min, ut.parse_hhmm(s.dep))) if v.dep_min is not None and s.dep else 300
            sc += pen * len(v.days ^ s.days)
            scored.append((sc, vi, si))
    scored.sort()
    uv, us, pairs = set(), set(), {}
    for _, vi, si in scored:
        if vi not in uv and si not in us:
            uv.add(vi)
            us.add(si)
            pairs[vi] = si
    return ([(vroutes[vi], slots[si]) for vi, si in sorted(pairs.items())],
            [v for vi, v in enumerate(vroutes) if vi not in uv],
            [s for si, s in enumerate(slots) if si not in us])


# =====================================================================================================================
# reconcile
# =====================================================================================================================
class _R:
    """Mutable working state of one reconcile run."""

    def __init__(self, cfg: "Config", vamsys: "VamsysSnapshot", evidence: Mapping[RouteKey, Evidence],
                 index: "ObsIndex", eff_time: str):
        self.cfg = cfg
        self.th = cfg.thresholds
        self.vamsys = vamsys
        self.index = index
        self.ev_cache: dict[RouteKey, Evidence] = dict(evidence)
        self.eff = eff_time
        self.hits: Counter = Counter()
        self.decisions: list[Decision] = []
        self.consumed: set[str] = set()
        self.warnings: list[str] = []

    def ev(self, key: RouteKey) -> Evidence:
        key = tuple(key)
        e = self.ev_cache.get(key)
        if e is None:
            e = build_evidence(key, self.index, self.cfg)
            self.ev_cache[key] = e
        return e

    # ---------------------------------------------------------------- decision factory
    def add(self, d: Decision) -> Decision:
        if d.category in REPORT_CATS and d.needs_review:
            # report-only rows are never exported; only a refetch request stays a meaningful review item
            d.review_flags = [f for f in d.review_flags if f == str(RC.NO_DATA_REFETCH)]
            d.needs_review = bool(d.review_flags)
        if d.vroute_id:
            self.consumed.add(d.vroute_id)
        self.decisions.append(d)
        return d

    def number(self, cs: str) -> str:
        return self.cfg.callsign_to_number(cs)

    def rec(self, op, orig, dest, rtype, fd2=(), ev: Evidence | None = None, vfleets=()) -> FleetRecommendation:
        return recommend_fleets(self.cfg, op, orig, dest, route_type=rtype, plan_fd2=fd2,
                                fr24_fleet_ids=ev.fleet_ids if ev else (), vamsys_fleet_ids=vfleets)

    def fr24_rec(self, op, orig, dest, rtype, ev: Evidence) -> FleetRecommendation:
        """Fleets for a route known only from FR24 (C: CREATE_FR24 / CHARTER / POSITIONING). Body tier from the
        filtered ``ev.fleet_ids`` (one-off substitutions below fr24_ac_min_share ignored), exactly what the route
        gets on the next run once it is in VAMSYS (B: _keep_unplanned) — passing the raw ``ev.ac_counter`` as plan
        types here made a single narrowbody substitution turn a widebody route MIXED, so the next run reported
        FLEET_EXTRA (not idempotent). The raw codes are only a fallback when none of them maps to a fleet."""
        return self.rec(op, orig, dest, rtype, fd2=() if ev.fleet_ids else list(ev.ac_counter), ev=ev)

    @staticmethod
    def apply_rec(d: Decision, rec: FleetRecommendation) -> None:
        for r in rec.reasons:
            d.add_reason(r.code, review=r.code in rec.review_flags, **r.params)

    def confidence(self, d: Decision, ev: Evidence | None) -> None:
        if d.needs_review or ev is None or ev.state == S.NO_DATA:
            d.confidence = Confidence.LOW
        elif ev.state == S.ACTIVE and ev.acts >= 5:
            d.confidence = Confidence.HIGH
        else:
            d.confidence = Confidence.MEDIUM

    def create(self, cat: DecisionCategory, key: RouteKey, rtype: str, slot_dep: str, slot_arr: str,
               days: Iterable[int], rec: FleetRecommendation, ev: Evidence | None, **kw) -> Decision:
        op, cs, orig, dest = key
        tgt = RouteTarget(op=op, callsign=cs, number=self.number(cs), orig=orig, dest=dest, type=rtype,
                          dep=slot_dep, arr=slot_arr, days=frozenset(days), fleet_ids=tuple(sorted(rec.fleet_ids)),
                          cost_index=self.cfg.default_cost_index.get(op),
                          start_date=self.eff if cat in CREATE_CATS else "")
        d = Decision(category=cat, target=tgt, evidence=EvidenceSummary.from_evidence(ev), **kw)
        self.apply_rec(d, rec)
        return d

    def vroute_decision(self, vr: VRoute, *, cat_change: DecisionCategory, cat_same: DecisionCategory,
                        key: RouteKey, rtype: str, dep: str, arr: str, days: frozenset[int],
                        rec: FleetRecommendation, ev: Evidence | None, bank: Bank | None = None,
                        extra_times: Sequence[tuple[str, str]] = (), **kw) -> Decision:
        """UPDATE/KEEP-style decision for an existing VAMSYS route."""
        op, cs, orig, dest = key
        cur = RouteTarget.from_vroute(vr)
        tol = self.th.time_tolerance_min
        if vr.dep and vr.arr and dep and arr:
            refs = [(dep, arr), *extra_times]
            if bank is not None:
                refs.append((bank.mode_dep, bank.mode_arr))
            if any(_d(vr.dep, a) <= tol and _d(vr.arr, b) <= tol for a, b in refs):
                if (vr.dep, vr.arr) != (dep, arr):
                    self.hits["time_tolerance_min"] += 1
                    if days:
                        days = ut.shift_days(days, _day_shift(dep, vr.dep))
                dep, arr = vr.dep, vr.arr
        elif not dep:
            dep, arr = vr.dep, vr.arr
        tgt = RouteTarget(op=op, callsign=cs, number=self.number(cs), orig=orig, dest=dest, type=rtype,
                          dep=dep, arr=arr, days=frozenset(days) if days else vr.days,
                          fleet_ids=tuple(sorted(rec.fleet_ids)), cost_index=cur.cost_index, hidden=cur.hidden,
                          start_date=cur.start_date, end_date="")
        changes = _changes(cur, tgt)
        d = Decision(category=cat_change if changes else cat_same, target=tgt, vroute_id=vr.id, current=cur,
                     changes=changes, evidence=EvidenceSummary.from_evidence(ev), **kw)
        if vr.has_end_date:
            d.add_reason(RC.END_DATE_CLEARED, review=True, end_date=vr.end_date)
        audit = audit_fleets(self.cfg, vr.fleet_ids, rec)
        for c in changes:
            if c.field not in ("fleet_ids", "end_date"):
                d.add_reason(RC.FIELD_CHANGED, field=c.field, old=c.old, new=c.new)
        for r in audit.reasons:
            d.add_reason(r.code, **r.params)
        self.apply_rec(d, rec)
        return d

    def retire(self, vr: VRoute, cat: DecisionCategory, ev: Evidence | None, code: str, review: bool = False,
               **params) -> Decision:
        cur = RouteTarget.from_vroute(vr)
        if vr.has_end_date:
            tgt = dataclasses.replace(cur)
            changes = []
        else:
            tgt = dataclasses.replace(cur, end_date=self.eff)
            changes = [FieldChange("end_date", "", self.eff)]
        d = Decision(category=cat, target=tgt, vroute_id=vr.id, current=cur, changes=changes,
                     evidence=EvidenceSummary.from_evidence(ev))
        d.add_reason(code, review=review, **params)
        if vr.has_end_date:
            d.add_reason(RC.ALREADY_RETIRING, end_date=vr.end_date)
        return d


def _changes(cur: RouteTarget, tgt: RouteTarget) -> list[FieldChange]:
    out = []
    for f in COMPARED_FIELDS:
        a, b = getattr(cur, f), getattr(tgt, f)
        if f == "days":
            if frozenset(a) != frozenset(b):
                out.append(FieldChange(f, sorted(a), sorted(b)))
        elif f == "fleet_ids":
            if tuple(sorted(a)) != tuple(sorted(b)):
                out.append(FieldChange(f, list(sorted(a)), list(sorted(b))))
        elif (a or "") != (b or ""):
            out.append(FieldChange(f, a, b))
    return out


def _select_fr24_banks(ev: Evidence, th) -> list[tuple[int, Bank]]:
    """Banks that become routes for FR24-driven keys (5.8 B/C): acts >= extra_slot_min_acts, else the top bank
    with an actual, else (schedules only) banks with >=2 recent obs, else the top bank."""
    b = list(enumerate(ev.banks))
    return ([x for x in b if x[1].acts >= th.extra_slot_min_acts] or [x for x in b if x[1].acts >= 1][:1]
            or [x for x in b if x[1].recent_obs >= 2] or b[:1])


def _assign_weekdays(ev: Evidence, sel: list[tuple[int, Bank]]) -> dict[int, frozenset[int]]:
    """One weekday -> one bank: every flown weekday of the key goes to the selected bank with most actuals on that
    weekday (ties: bank acts). Schedules-only keys use scheduled weekdays."""
    if not sel:
        return {}
    ref = sel[0][1].mode_dep                                   # common frame; results converted per bank
    acts, _, _, _, _ = _key_frame_stats(ev, ref, None)
    days = {wd for wd, n in acts.items() if n} or {
        (wd + _day_shift(b.mode_dep, ref)) % 7 for b in ev.banks for wd in b.days}
    won: dict[int, set[int]] = defaultdict(set)
    for wd in sorted(days):
        def score(ib):
            i, b = ib
            k = _day_shift(b.mode_dep, ref)
            return (_shift_wd(b.day_acts, k).get(wd, 0), (wd - k) % 7 in b.days, b.acts, -i)
        best = max(sel, key=score)
        won[best[0]].add(wd)
    return {i: ut.shift_days(won[i], _day_shift(ref, b.mode_dep)) for i, b in sel}


def _fr24_days(base: frozenset[int], won: frozenset[int], ev: Evidence, th, hits: Counter,
               frame_dep: str = "") -> tuple[frozenset[int], list]:
    """Weekdays for an existing route of an FR24-driven key: (base ∪ add) − drop where add = won weekdays seen in
    >= weekday_add_min_weeks distinct weeks, drop = weekdays with 0 observations of the key (only after
    >= weekday_drop_min_weeks observed weeks). Closure operation -> idempotent."""
    reasons = []
    acts, weeks, recent, t0, t1 = _key_frame_stats(ev, frame_dep, ev.info.get("recent_start_ts"))
    add = {wd for wd in won if len(weeks.get(wd, ())) >= th.weekday_add_min_weeks and recent.get(wd, 0) >= 1}
    drop = set()
    if ev.season_weeks_observed >= th.weekday_drop_min_weeks and t0 is not None:
        drop = {wd for wd in range(7) if not acts.get(wd)}       # 0 actual flights of the key on that weekday
        keep = (base | add) - drop
        if drop & (base | add) and not _coverage_ok(keep, {wd: len(s) for wd, s in weeks.items()}, t0, t1, th):
            hits["weekday_drop_min_coverage(kept)"] += 1
            drop = set()
    new = (base | add) - drop
    if not new:
        new = won or base
    if new != base:
        if add - base:
            reasons.append(Reason(RC.WEEKDAY_ADDED, {"old": sorted(base), "new": sorted(new)}))
            hits["weekday_add_min_weeks"] += 1
        if base & drop:
            reasons.append(Reason(RC.WEEKDAY_DROPPED, {"weeks": ev.season_weeks_observed, "old": sorted(base),
                                                       "new": sorted(new)}))
            hits["weekday_drop_min_weeks"] += 1
    return frozenset(new), reasons


def _callsign_ok(cs: str, has_number: bool = False) -> bool:
    """Regular flight callsign. Zero-padded / short numbers (CCA051 = CA51) are regular when FR24 publishes a flight
    number for them; letter suffixes (CCA123R) and 0 are positioning/test callsigns."""
    digits = re.sub(r"^[A-Z]+", "", cs)
    if not re.fullmatch(r"\d{1,4}", digits) or int(digits) == 0:
        return False
    if digits.startswith("0") or len(digits) <= 2:
        return has_number
    return True


def reconcile(cfg: "Config", plan: PlanData, groups: Mapping[RouteKey, PlanGroup],
              rotation_gaps: Mapping[RouteKey, RotationGap], vamsys: "VamsysSnapshot",
              evidence: Mapping[RouteKey, Evidence], index: "ObsIndex", *, now: int | None = None,
              airport_names: Mapping[str, str] | None = None) -> BuildResult:
    """Pure function: all inputs in, decisions out (no I/O). See module doc for the table."""
    now_dt = ut.ts_to_dt(now) if now is not None else _dt.datetime.now(ut.UTC)
    eff = cfg.effective_time(now_dt)
    R = _R(cfg, vamsys, evidence, index, eff)
    th = cfg.thresholds
    alias = cfg.airport_alias
    comm = vamsys.commercial_routes
    v_by_key: dict[RouteKey, list[VRoute]] = defaultdict(list)
    for v in comm:
        v_by_key[v.key].append(v)
    gaps_v: dict[RouteKey, RotationGap] = {
        (g.op, g.out_callsign, alias.get(g.orig, g.orig), alias.get(g.dest, g.dest)): g for g in rotation_gaps.values()}

    # ------------------------------------------------------------------ 5.6 single-day + carrier change -> work list
    work: list[tuple[RouteKey, RouteKey, PlanGroup, bool]] = []   # (target_key, raw_key, group, promoted)
    target_keys: set[RouteKey] = set(groups)
    for rk in sorted(groups):
        g = groups[rk]
        ev = R.ev(rk)
        promoted = False
        if g.is_single_day_only:
            if ev.state == S.ACTIVE and ev.acts >= th.single_day_promote_acts:
                promoted = True
                R.hits["single_day_promote_acts"] += 1
            else:
                op, cs, o, dd = rk
                d = Decision(category=C.WATCH_PLAN, group_key=rk, plan_refs=[l.src_ref for l in g.legs],
                             target=RouteTarget(op=op, callsign=cs, number=g.number, orig=o, dest=dd,
                                                type=g.route_type, dep=g.raw_slots[0].dep, arr=g.raw_slots[0].arr,
                                                days=g.raw_slots[0].days),
                             evidence=EvidenceSummary.from_evidence(ev))
                d.add_reason(RC.SINGLE_DAY_IGNORED, why=f"本季实飞{ev.acts}班<{th.single_day_promote_acts}")
                d.details["single_day"] = True
                R.confidence(d, ev)
                R.add(d)
                continue
        tk = rk
        if ev.state == S.CARRIER_CHANGED:
            nk = (ev.info["new_op"], ev.info["new_callsign"], rk[2], rk[3])
            if nk in target_keys:
                R.warnings.append(f"{rk}: 换承运目标 {nk} 已是计划组，不重定向")
            else:
                tk = nk
                target_keys.add(nk)
                R.hits["carrier_change_min_acts"] += 1
        work.append((tk, rk, g, promoted))

    rs = index.recent_start_ts
    # ------------------------------------------------------------------ A. plan groups x VAMSYS
    plan_target_keys = {tk for tk, _, _, _ in work}
    for tk, rk, g, promoted in work:
        op, cs, orig, dest = tk
        ev = R.ev(tk)
        rtype = "cargo" if cfg.is_cargo_op(op) else g.route_type
        v_cands = [v for v in v_by_key.get(tk, []) if v.id not in R.consumed]
        via_old = False
        if not v_cands and tk != rk:
            v_cands = [v for v in v_by_key.get(rk, []) if v.id not in R.consumed]
            via_old = bool(v_cands)
        refs = [l.src_ref for l in g.legs]
        common = {"group_key": rk}

        def notes(d: Decision) -> None:
            if tk != rk:
                d.add_reason(RC.CARRIER_CHANGED, new_callsign=cs, old_callsign=rk[1])
                if via_old:
                    d.add_reason(RC.MATCHED_VIA_OLD_OPERATOR, old_callsign=rk[1])
            if promoted:
                d.add_reason(RC.SINGLE_DAY_PROMOTED, acts=ev.acts)
            if ev.state == S.ACTIVE and ev.other_ops:
                others = {f"{a}/{b}": n for a, b, n in ev.other_ops}
                d.add_reason(RC.MIXED_CARRIER, op=op, acts=ev.acts, others=others)
            if ev.state == S.NO_DATA:
                d.add_reason(RC.NO_DATA_REFETCH, review=True)

        confirmed = _is_confirmed(ev)
        slots = calibrate_slots(cfg, g, ev if confirmed else None, v_cands, promoted=promoted, hits=R.hits)
        live_v = [v for v in v_cands if not v.has_end_date] or v_cands
        vfleets = frozenset().union(*(v.fleet_ids for v in live_v)) if live_v else frozenset()
        rec = R.rec(op, orig, dest, rtype, fd2=g.fd2_codes, ev=ev, vfleets=vfleets)

        def slot_reasons(d: Decision, s: CalibratedSlot) -> None:
            for r in s.reasons:
                d.add_reason(r.code, review=r.code in s.review_flags, **r.params)

        def slot_bank(s: CalibratedSlot):
            return ev.banks[s.bank_index] if (s.bank_index is not None and ev.banks) else None

        def slot_recent(s: CalibratedSlot) -> bool:
            """5.8 creates need >=1 actual in the recent window (FR24-derived slots: in that bank)."""
            b = slot_bank(s)
            if s.from_fr24_extra and b is not None:
                return _bank_recent_on(b, None, rs)
            return ev.recent_acts >= 1

        def slot_last(s: CalibratedSlot) -> str:
            b = slot_bank(s)
            t = b.act_ts[-1] if (s.from_fr24_extra and b is not None and b.act_ts) else ev.last_act
            return ut.ts_utc_date(t) if t else ""

        def confirm(d: Decision) -> None:
            if ev.state == S.SCHED_ONLY:
                d.add_reason(RC.PLAN_FR24_SCHEDULED, obs=ev.obs)
            else:
                d.add_reason(RC.PLAN_FR24_CONFIRMED, acts=ev.acts)

        if not v_cands:
            for i, s in enumerate(slots, 1):
                fresh_ok = slot_recent(s)
                cat = C.CREATE_PLAN if (ev.state == S.ACTIVE and not s.unflown and fresh_ok) else C.WATCH_PLAN
                d = R.create(cat, tk, rtype, s.dep, s.arr, s.days, rec, ev, plan_refs=s.src_refs or refs,
                             slot_index=f"{i}/{len(slots)}", **common)
                if s.from_fr24_extra:
                    d.details["fr24_extra_slot"] = True
                if cat == C.CREATE_PLAN:
                    confirm(d)
                elif s.unflown:
                    d.add_reason(RC.PLAN_SLOT_NOT_FLOWN, dep=s.dep, arr=s.arr, weeks=ev.season_weeks_observed)
                    R.hits["weekday_drop_min_weeks(slot)"] += 1
                elif ev.state == S.ACTIVE:
                    d.add_reason(RC.NOT_RECENT, last_date=slot_last(s), days=th.recent_window_days)
                    R.hits["recent_window_days(create)"] += 1
                else:
                    d.add_reason(RC.PLAN_NOT_FLOWN, state=str(ev.state))
                slot_reasons(d, s)
                notes(d)
                d.details.update(plan_dep=s.plan_dep, plan_arr=s.plan_arr, plan_days=sorted(s.plan_days))
                R.confidence(d, ev)
                R.add(d)
            continue

        if not confirmed:
            for v in v_cands:
                d = R.retire(v, C.RETIRE_DEFERRED, ev, RC.DEFERRED_PLAN_NOT_FLOWN, state=str(ev.state))
                d.group_key, d.plan_refs = rk, refs
                notes(d)
                R.confidence(d, ev)
                R.add(d)
            continue

        # pairing: routes without End Date first, then (only for leftover slots) end-dated ones
        fresh = [v for v in v_cands if not v.has_end_date]
        ending = [v for v in v_cands if v.has_end_date]
        pairs, un_v, un_s = pair_slots(cfg, fresh, slots)
        pairs2, un_v2, un_s = pair_slots(cfg, ending, un_s)
        pairs += pairs2
        un_v += un_v2
        for (v, s) in pairs:
            if v.days != s.days:
                R.hits["slot_pair_day_penalty"] += 1
        n = len(slots)
        idx_of = {id(s): i for i, s in enumerate(slots, 1)}
        for v, s in sorted(pairs, key=lambda p: idx_of[id(p[1])]):
            bank = ev.banks[s.bank_index] if s.bank_index is not None else None
            d = R.vroute_decision(v, cat_change=C.UPDATE, cat_same=C.KEEP, key=tk, rtype=rtype, dep=s.dep,
                                  arr=s.arr, days=s.days, rec=rec, ev=ev, bank=bank, plan_refs=s.src_refs or refs,
                                  slot_index=f"{idx_of[id(s)]}/{n}", **common)
            confirm(d)
            slot_reasons(d, s)
            notes(d)
            d.details.update(plan_dep=s.plan_dep, plan_arr=s.plan_arr, plan_days=sorted(s.plan_days))
            R.confidence(d, ev)
            R.add(d)
        used_banks = {s.bank_index for s in slots if s.bank_index is not None}
        for v in un_v:
            hit = None
            if not v.has_end_date and v.dep:
                for bi, b in enumerate(ev.banks):
                    if bi not in used_banks and _within(th.vamsys_bank_match, v.dep, v.arr, b.mode_dep, b.mode_arr):
                        hit = (bi, b)
                        break
            if hit is not None:
                bi, b = hit
                used_banks.add(bi)
                R.hits["vamsys_bank_match"] += 1
                d = R.vroute_decision(v, cat_change=C.UPDATE, cat_same=C.KEEP, key=tk, rtype=rtype,
                                      dep=b.mode_dep, arr=b.mode_arr, days=b.act_days or b.days, rec=rec, ev=ev,
                                      bank=b, plan_refs=refs, **common)
                d.add_reason(RC.VAMSYS_MATCHES_BANK, bank=b.label)
            else:
                d = R.retire(v, C.RETIRE_REDUNDANT, ev, RC.VAMSYS_REDUNDANT)
                d.group_key, d.plan_refs = rk, refs
            notes(d)
            R.confidence(d, ev)
            R.add(d)
        for s in un_s:
            if s.unflown:
                d = R.create(C.WATCH_PLAN, tk, rtype, s.dep, s.arr, s.days, rec, ev, plan_refs=s.src_refs or refs,
                             slot_index=f"{idx_of[id(s)]}/{n}", **common)
                d.add_reason(RC.PLAN_SLOT_NOT_FLOWN, dep=s.dep, arr=s.arr, weeks=ev.season_weeks_observed)
                R.hits["weekday_drop_min_weeks(slot)"] += 1
                slot_reasons(d, s)
                notes(d)
                R.confidence(d, ev)
                R.add(d)
                continue
            if not slot_recent(s):
                d = R.create(C.WATCH_PLAN, tk, rtype, s.dep, s.arr, s.days, rec, ev, plan_refs=s.src_refs or refs,
                             slot_index=f"{idx_of[id(s)]}/{n}", **common)
                d.add_reason(RC.NOT_RECENT, last_date=slot_last(s), days=th.recent_window_days)
                R.hits["recent_window_days(create)"] += 1
                slot_reasons(d, s)
                notes(d)
                R.confidence(d, ev)
                R.add(d)
                continue
            d = R.create(C.CREATE_EXTRA_SLOT, tk, rtype, s.dep, s.arr, s.days, rec, ev, plan_refs=s.src_refs or refs,
                         slot_index=f"{idx_of[id(s)]}/{n}", **common)
            confirm(d)
            slot_reasons(d, s)
            notes(d)
            d.details.update(plan_dep=s.plan_dep, plan_arr=s.plan_arr, plan_days=sorted(s.plan_days),
                             vamsys_routes=len(v_cands))
            R.confidence(d, ev)
            R.add(d)

    # ------------------------------------------------------------------ B. VAMSYS routes not in the plan
    rest: dict[RouteKey, list[VRoute]] = defaultdict(list)
    for v in comm:
        if v.id not in R.consumed:
            rest[v.key].append(v)
    plan_by_num_od: dict[tuple[str, str, str], list[RouteKey]] = defaultdict(list)
    for tk in plan_target_keys:
        plan_by_num_od[(R.number(tk[1]), tk[2], tk[3])].append(tk)
    for key in sorted(rest):
        vs = rest[key]
        op, cs, orig, dest = key
        ev = R.ev(key)
        num = R.number(cs)
        alt_plan = [k for k in plan_by_num_od.get((num, orig, dest), []) if k[0] != op]
        alt_acts = sum(1 for o in index.by_number_od.get((num, orig, dest), ()) if o.op != op and o.is_actual)
        occasional = ev.state == S.ACTIVE and bool(alt_plan) and ev.acts <= 2 and alt_acts >= 5
        if ev.state in (S.ACTIVE, S.SCHED_ONLY) and not occasional:
            _keep_unplanned(R, key, vs, ev)
            continue
        if key in gaps_v and ev.acts == 0:
            g = gaps_v[key]
            for v in vs:
                cur = RouteTarget.from_vroute(v)
                d = Decision(category=C.HOLD, target=cur, vroute_id=v.id, current=cur,
                             evidence=EvidenceSummary.from_evidence(ev))
                d.add_reason(RC.HOLD_ROTATION_GAP_BLIND)
                d.add_reason(RC.ROTATION_GAP_LEG, out_callsign=g.out_callsign, ret_callsign=g.ret_callsign)
                R.confidence(d, ev)
                R.add(d)
            continue
        for v in vs:
            if occasional:
                d = R.retire(v, C.RETIRE, ev, RC.RETIRE_OCCASIONAL_OLD_CARRIER, acts=ev.acts)
            elif ev.state == S.CARRIER_CHANGED:
                d = R.retire(v, C.RETIRE, ev, RC.RETIRE_CARRIER_CHANGED, new_callsign=ev.info.get("new_callsign"))
            elif ev.state == S.ROUTE_CHANGED:
                d = R.retire(v, C.RETIRE, ev, RC.RETIRE_ROUTE_CHANGED,
                             other_ods=[f"{a}-{b}" for a, b, _ in ev.other_ods[:3]])
            elif ev.state == S.STOPPED_PRE_SEASON:
                d = R.retire(v, C.RETIRE, ev, RC.RETIRE_STOPPED_PRE_SEASON, last_date=ut.ts_utc_date(ev.pre_last_act),
                             kind=str(ev.stopped_kind))
            elif ev.state == S.STALE_SCHED:
                d = R.retire(v, C.RETIRE, ev, RC.RETIRE_STALE_SCHED, last_date=ut.ts_utc_date(ev.last_obs))
                R.hits["recent_window_days"] += 1
            elif ev.state == S.NEVER_FLOWN:
                d = R.retire(v, C.RETIRE, ev, RC.RETIRE_NEVER_FLOWN, days=th.never_flown_lookback_days)
            else:  # NO_DATA: data blind spot -> hold, refetch
                cur = RouteTarget.from_vroute(v)
                d = Decision(category=C.HOLD, target=cur, vroute_id=v.id, current=cur,
                             evidence=EvidenceSummary.from_evidence(ev))
                d.add_reason(RC.NO_DATA_REFETCH, review=True)
            if ev.state not in (S.CARRIER_CHANGED,) and "carrier_change_min_acts" in ev.info.get("threshold_blocked", []):
                R.hits["carrier_change_min_acts(blocked)"] += 1
            R.confidence(d, ev)
            R.add(d)

    # ------------------------------------------------------------------ C. FR24 only
    known = set(groups) | plan_target_keys | {v.key for v in vamsys.routes}
    planned_by_num_od: dict[tuple[str, str, str], set[RouteKey]] = defaultdict(set)
    for k in plan_target_keys | {v.key for v in comm}:
        planned_by_num_od[(R.number(k[1]), k[2], k[3])].add(k)
    for key in sorted(k for k in index.by_key if k not in known):
        obs = index.by_key[key]
        if all(o.op_source == "airline_fallback" for o in obs):
            continue
        ev = R.ev(key)
        if ev.acts == 0:
            continue
        op, cs, orig, dest = key
        is_gap = key in gaps_v
        acts = [o for o in ev.observations if o.is_actual]
        dates = sorted({ut.ts_utc_date(o.ev_ts) for o in acts})
        regs = sorted({o.reg for o in acts if o.reg})
        rtype = "cargo" if cfg.is_cargo_op(op) else "scheduled"
        if not _callsign_ok(cs, any(o.number for o in acts)):
            d = R.create(C.POSITIONING, key, rtype,
                         ev.banks[0].mode_dep if ev.banks else "", ev.banks[0].mode_arr if ev.banks else "",
                         frozenset(ut.ts_utc_weekday(o.ev_ts) for o in acts),
                         R.fr24_rec(op, orig, dest, rtype, ev), ev)
            d.reasons.clear()
            d.needs_review, d.review_flags = False, []
            d.add_reason(RC.POSITIONING_FLIGHT, why=f"呼号{cs}不规范")
            d.evidence.dates, d.evidence.regs = dates, regs
            R.confidence(d, ev)
            R.add(d)
            continue
        weeks = _act_weeks(ev, index.season_start_ts)
        ok = (ev.acts >= th.rotation_gap_add_min_acts) if is_gap else (
            ev.acts >= th.fr24_add_min_acts and weeks >= th.fr24_add_min_weeks)

        def report(code, **params) -> Decision:
            d = R.create(C.CHARTER, key, rtype, ev.banks[0].mode_dep if ev.banks else "",
                         ev.banks[0].mode_arr if ev.banks else "",
                         frozenset(ut.ts_utc_weekday(o.sched_dep or o.ev_ts) for o in acts),
                         R.fr24_rec(op, orig, dest, rtype, ev), ev)
            d.reasons.clear()
            d.needs_review, d.review_flags = False, []
            d.add_reason(code, **params)
            d.evidence.dates, d.evidence.regs = dates, regs
            d.details.update(acts=ev.acts, weeks=weeks, recent_acts=ev.recent_acts,
                             first=dates[0] if dates else "", last=dates[-1] if dates else "")
            R.confidence(d, ev)
            return R.add(d)

        # mixed operation: same number + OD flown by another target op that is planned / in VAMSYS
        mains = [k for k in planned_by_num_od.get((R.number(cs), orig, dest), ()) if k[0] != op]
        if mains:
            main = max(mains, key=lambda k: (R.ev(k).recent_acts, R.ev(k).acts, k))
            mev = R.ev(main)
            if not (ok and ev.recent_acts > mev.recent_acts):
                report(RC.MIXED_OP_MINORITY, callsign=cs, acts=ev.acts, recent=ev.recent_acts, main=main[1],
                       main_recent=mev.recent_acts)
                R.hits["mixed_op_minority"] += 1
                for md in R.decisions:
                    if md.target.key == main and not any(r.code == RC.MIXED_CARRIER for r in md.reasons):
                        md.add_reason(RC.MIXED_CARRIER, op=main[0], acts=mev.acts, others={f"{op}/{cs}": ev.acts})
                continue
        if not ok or not ev.banks:
            report(RC.CHARTER_IRREGULAR, days=len(dates), dates=dates[:6])
            if ev.acts >= th.fr24_add_min_acts or weeks >= th.fr24_add_min_weeks:
                R.hits["fr24_add_min_acts/min_weeks"] += 1
            continue
        rec = R.fr24_rec(op, orig, dest, rtype, ev)
        sel = _select_fr24_banks(ev, th)
        won = _assign_weekdays(ev, sel)
        sel = [(i, b) for i, b in sel if won.get(i) and _bank_recent_on(b, None, rs)]
        if not sel or ev.recent_acts < 1:
            report(RC.NOT_RECENT, last_date=ut.ts_utc_date(ev.last_act), days=th.recent_window_days)
            R.hits["recent_window_days(create)"] += 1
            continue
        for n_i, (bi, b) in enumerate(sel, 1):
            d = R.create(C.CREATE_FR24, key, rtype, b.mode_dep, b.mode_arr, won[bi], rec, ev,
                         slot_index=f"{n_i}/{len(sel)}")
            d.add_reason(RC.FR24_ONLY_ROUTE, acts=ev.acts, weeks=weeks)
            if is_gap:
                g = gaps_v[key]
                d.add_reason(RC.ROTATION_GAP_LEG, out_callsign=g.out_callsign, ret_callsign=g.ret_callsign)
                R.hits["rotation_gap_add_min_acts"] += 1
            else:
                R.hits["fr24_add_min_acts/min_weeks"] += 1
            if ev.other_ops:
                d.add_reason(RC.MIXED_CARRIER, op=op, acts=ev.acts, others={f"{a}/{c}": n for a, c, n in ev.other_ops})
            if b.superseded:
                d.add_reason(RC.BANK_SUPERSEDED, old=[f"{x['mode_dep']}-{x['mode_arr']}" for x in b.superseded])
            d.evidence.regs = regs
            R.confidence(d, ev)
            R.add(d)

    # ------------------------------------------------------------------ D. airports
    names = dict(airport_names or {})
    names.update(cfg.airport_names)
    missing: dict[str, list[Decision]] = defaultdict(list)
    for d in R.decisions:
        if d.category in REPORT_CATS or d.category in RETIRE_CATS or d.category == C.HOLD:
            continue
        for ap in (d.target.orig, d.target.dest):
            if ap and not vamsys.has_airport(ap):
                missing[ap].append(d)
    for ap in sorted(missing):
        for d in missing[ap]:
            if ap not in d.depends_on_airports:
                d.depends_on_airports.append(ap)
        a = Decision(category=C.AIRPORT_ADD, target=RouteTarget(op="", callsign="", number="", orig=ap, dest="",
                                                                type=""),
                     details={"name": names.get(ap, ""), "routes": len(missing[ap])})
        a.add_reason(RC.AIRPORT_MISSING, review=not a.details["name"], airports=[ap])
        R.decisions.append(a)
    for ap_list in {id(d): d for ds in missing.values() for d in ds}.values():
        ap_list.add_reason(RC.DEPENDS_ON_AIRPORT_ADD, airports=list(ap_list.depends_on_airports))

    # ------------------------------------------------------------------ meta
    decisions = R.decisions
    counts = Counter(str(d.category) for d in decisions)
    meta = RunMeta(
        season=cfg.season_name, season_label=cfg.season_label,
        generated_at=now_dt.astimezone(ut.UTC).strftime("%Y-%m-%d %H:%M:%S"), effective_time=eff,
        season_start=cfg.start_date.isoformat(), season_end=cfg.end_date.isoformat(),
        plan_rows=len(plan.rows), plan_legs=len(plan.legs), plan_numbers=len(plan.numbers),
        vamsys_revision=vamsys.revision, vamsys_storage_revision=vamsys.storage_revision,
        vamsys_routes=len(vamsys.routes), vamsys_commercial_routes=len(comm),
        fr24_rows=len(index.obs), fr24_latest_ts=index.latest_ts,
        thresholds={**{k: (list(map(list, v)) if k == "bank_merge" else (list(v) if isinstance(v, tuple) else v))
                       for k, v in th.as_dict().items()}, "weekday_drop_min_coverage": _drop_coverage(th)},
        threshold_hits=dict(sorted(R.hits.items())), counts=dict(sorted(counts.items())),
        warnings=list(R.warnings),
    )
    res = BuildResult(decisions=decisions, meta=meta, threshold_hits=R.hits, warnings=list(R.warnings))
    res.target_states = target_states_from(R.ev_cache, decisions, cfg)
    return res


def _keep_unplanned(R: _R, key: RouteKey, vs: list[VRoute], ev: Evidence) -> None:
    """5.8 B: VAMSYS route not in the plan but flown/scheduled in FR24 -> calibrate to FR24 (UPDATE / KEEP_UNPLANNED)."""
    cfg, th = R.cfg, R.th
    op, cs, orig, dest = key
    rtype = "cargo" if cfg.is_cargo_op(op) else (vs[0].type if vs[0].type in ("scheduled", "cargo") else "scheduled")
    live_v = [v for v in vs if not v.has_end_date] or vs
    vfleets = frozenset().union(*(v.fleet_ids for v in live_v))
    rec = R.rec(op, orig, dest, rtype, ev=ev, vfleets=vfleets)
    sel = _select_fr24_banks(ev, th) if ev.banks else []
    won = _assign_weekdays(ev, sel)
    fake_slots = [CalibratedSlot(dep=b.mode_dep, arr=b.mode_arr, days=won.get(i) or b.act_days or b.days,
                                 bank_index=i) for i, b in sel]
    fresh = [v for v in vs if not v.has_end_date]
    ending = [v for v in vs if v.has_end_date]
    pairs, un_v, un_s = pair_slots(cfg, fresh, fake_slots)
    pairs2, un_v2, _ = pair_slots(cfg, ending, un_s)
    pairs += pairs2
    un_v += un_v2
    used = {s.bank_index for _, s in pairs}

    def emit(v: VRoute, bank: Bank | None, won_days: frozenset[int], extra: list[Reason]) -> None:
        if bank is None:
            dep, arr, days, rs = v.dep, v.arr, v.days, []
        else:
            base = ut.shift_days(v.days, _day_shift(v.dep, bank.mode_dep)) if v.dep else v.days
            days, rs = _fr24_days(base, won_days, ev, th, R.hits, frame_dep=bank.mode_dep)
            dep, arr = bank.mode_dep, bank.mode_arr
        d = R.vroute_decision(v, cat_change=C.UPDATE, cat_same=C.KEEP_UNPLANNED, key=key, rtype=rtype, dep=dep,
                              arr=arr, days=days, rec=rec, ev=ev, bank=bank)
        d.add_reason(RC.UNPLANNED_ACTIVE, state=str(ev.state))
        for r in rs + extra:
            d.add_reason(r.code, review=r.code == RC.WEEKDAY_DROPPED, **r.params)
        if ev.other_ops and ev.state == S.ACTIVE:
            d.add_reason(RC.MIXED_CARRIER, op=op, acts=ev.acts, others={f"{a}/{c}": n for a, c, n in ev.other_ops})
        R.confidence(d, ev)
        R.add(d)

    for v, s in pairs:
        emit(v, ev.banks[s.bank_index], won.get(s.bank_index, frozenset()), [])
    for v in un_v:
        hit = None
        if not v.has_end_date and v.dep:
            for bi, b in enumerate(ev.banks):
                if bi not in used and _within(th.vamsys_bank_match, v.dep, v.arr, b.mode_dep, b.mode_arr):
                    hit = (bi, b)
                    break
        if hit is not None:
            used.add(hit[0])
            R.hits["vamsys_bank_match"] += 1
            emit(v, hit[1], hit[1].act_days or hit[1].days, [Reason(RC.VAMSYS_MATCHES_BANK, {"bank": hit[1].label})])
        elif not ev.banks:
            emit(v, None, frozenset(), [])
        else:
            d = R.retire(v, C.RETIRE_REDUNDANT, ev, RC.VAMSYS_REDUNDANT)
            R.confidence(d, ev)
            R.add(d)


# =====================================================================================================================
# closure / apply / targets
# =====================================================================================================================
def check_closure(decisions: Iterable[Decision], vamsys: "VamsysSnapshot") -> list[str]:
    """Plan 5.8 E / 8.1: each commercial VAMSYS route id in exactly one VROUTE_CATEGORIES decision; returns
    error strings (empty = closed)."""
    errs = []
    comm = {r.id for r in vamsys.commercial_routes}
    seen = Counter()
    for d in decisions:
        if d.category in VROUTE_CATEGORIES:
            if not d.vroute_id:
                errs.append(f"{d.category} 决策缺少 vroute_id: {d.target.key}")
                continue
            seen[d.vroute_id] += 1
            if d.vroute_id not in comm:
                errs.append(f"{d.category} 引用的 {d.vroute_id} 不是 VAMSYS 商业航线")
        elif d.vroute_id:
            errs.append(f"{d.category} 决策不应带 vroute_id ({d.vroute_id})")
    for rid, n in sorted(seen.items()):
        if n > 1:
            errs.append(f"VAMSYS 航线 {rid} 出现在 {n} 条决策中")
    for rid in sorted(comm - set(seen)):
        errs.append(f"VAMSYS 航线 {rid} 没有去向")
    if not errs and len(seen) != len(comm):
        errs.append(f"去向总数 {len(seen)} ≠ 商业航线数 {len(comm)}")
    return errs


def _vroute_from_target(base: VRoute | None, t: RouteTarget, rid: str) -> VRoute:
    dep_min, arr_min = ut.parse_hhmm(t.dep), ut.parse_hhmm(t.arr)
    ci = "" if t.cost_index is None else str(t.cost_index)
    if base is None:
        return VRoute(id=rid, op=t.op, callsign=t.callsign, number=t.number, orig=t.orig, dest=t.dest, type=t.type,
                      dep=t.dep, arr=t.arr, dep_min=dep_min, arr_min=arr_min, days=frozenset(t.days),
                      fleet_ids=frozenset(t.fleet_ids), start_date=t.start_date, end_date=t.end_date,
                      cost_index=ci, hidden=t.hidden, tags="", is_special=False)
    return dataclasses.replace(base, op=t.op, callsign=t.callsign, number=t.number, orig=t.orig, dest=t.dest,
                               type=t.type, dep=t.dep, arr=t.arr, dep_min=dep_min, arr_min=arr_min,
                               days=frozenset(t.days), fleet_ids=frozenset(t.fleet_ids), end_date=t.end_date,
                               cost_index=ci if t.cost_index is not None else base.cost_index, hidden=t.hidden)


def apply_decisions(routes: Sequence[VRoute], decisions: Iterable[Decision]) -> list[VRoute]:
    """In-memory application of decisions to VAMSYS routes (for the idempotency test, plan 8.2)."""
    by_id = {r.id: r for r in routes}
    order = [r.id for r in routes]
    new: list[VRoute] = []
    for i, d in enumerate(decisions):
        if d.category in CREATE_CATS:
            new.append(_vroute_from_target(None, d.target, f"new:{i}"))
        elif d.category in (C.UPDATE,) and d.vroute_id in by_id:
            by_id[d.vroute_id] = _vroute_from_target(by_id[d.vroute_id], d.target, d.vroute_id)
        elif d.category in RETIRE_CATS and d.vroute_id in by_id and d.changes:
            by_id[d.vroute_id] = dataclasses.replace(by_id[d.vroute_id], end_date=d.target.end_date)
    return [by_id[i] for i in order] + new


def effective_changes(decisions: Iterable[Decision]) -> Counter:
    """Count decisions that would change VAMSYS (CREATE_*, UPDATE with changes, RETIRE* with changes)."""
    c = Counter()
    for d in decisions:
        if d.category in CREATE_CATS or (d.category == C.UPDATE and d.changes) or (d.category in RETIRE_CATS and d.changes):
            c[str(d.category)] += 1
    return c


_UNCERTAIN_CATS = RETIRE_CATS | CREATE_CATS | {C.WATCH_PLAN, C.CHARTER, C.HOLD}


def target_states_from(evidence: Mapping[RouteKey, Evidence], decisions: Iterable[Decision],
                       cfg: "Config") -> list[TargetState]:
    """Plan 6.2 P1/P2 tiers for flight-number targets based on this build.
    P1 (uncertain): any key of the number not ACTIVE, recent actuals < carrier_change_min_acts, or a decision in
    retire/deferred/create/watch/charter/hold, or needs_review. Otherwise P2."""
    th = cfg.thresholds
    state: dict[str, str] = {}
    why: dict[str, list[str]] = defaultdict(list)
    nums: set[str] = set()
    for d in decisions:
        if d.category in (C.AIRPORT_ADD, C.POSITIONING) or not d.target.callsign:
            continue
        num = cfg.callsign_to_number(d.target.callsign)
        if not num.startswith(cfg.number_prefix):
            continue
        nums.add(num)
        if d.category in _UNCERTAIN_CATS:
            why[num].append(str(d.category))
        elif d.needs_review:
            why[num].append("review")
        if d.evidence.state:
            state.setdefault(num, d.evidence.state)
    for key, ev in evidence.items():
        num = cfg.callsign_to_number(key[1])
        if num not in nums:
            continue
        if ev.state != S.ACTIVE:
            why[num].append(str(ev.state))
        elif ev.recent_acts < th.carrier_change_min_acts:
            why[num].append("recent_acts<min")
        if ev.state == S.ACTIVE:
            state[num] = str(ev.state)
    out = []
    for num in sorted(nums):
        r = sorted(set(why.get(num, ())))
        out.append(TargetState(query=num, kind=TargetKind.FLIGHT, priority=Priority.P1 if r else Priority.P2,
                               last_state=state.get(num, ""), reason=",".join(r)))
    return out


# =====================================================================================================================
# data sources
# =====================================================================================================================
def _load_fr24_rows(cfg: "Config", log: LogFn) -> tuple[list[dict], dict[str, Any], str]:
    """Rows from FR24Store (fr24_cache.db, the only FR24 source); no rows when it is missing/empty. Never creates
    the DB."""
    if cfg.cache_db.is_file():
        try:
            from .fr24_store import FR24Store
            with FR24Store.open(cfg, readonly=True) as st:
                n = st.count_rows()
                if n:
                    rows = list(st.iter_rows())
                    extras: dict[str, Any] = {"search_index": st.search_index() or {}, "fetched_numbers": set(),
                                              "last_fetch": "", "airport_names": {}, "iata_to_icao": {}}
                    try:
                        extras["iata_to_icao"] = st.iata_to_icao()
                        extras["airport_names"] = {k: v[1] for k, v in st.airport_info().items() if v and v[1]}
                        extras["fetched_numbers"] = {t.query for t in st.iter_targets(TargetKind.FLIGHT) if t.last_ok_at}
                        stt = st.stats(cfg)
                        if stt.last_fetch_at:
                            extras["last_fetch"] = ut.ts_utc_str(stt.last_fetch_at)
                    except Exception as e:  # noqa: BLE001 - optional extras
                        log(f"FR24 缓存附加信息读取失败（忽略）：{e}")
                    log(f"FR24 数据源：{cfg.cache_db}（{n} 行）")
                    return rows, extras, "store"
                log(f"!! FR24 缓存 {cfg.cache_db} 为空（先运行 fetch，或在数据页导入 fr24_cache.db）")
        except Exception as e:  # noqa: BLE001
            log(f"!! FR24 缓存 {cfg.cache_db} 打开失败：{e}")
    else:
        log(f"!! FR24 缓存 {cfg.cache_db} 不存在（先运行 fetch，或在数据页导入 fr24_cache.db）")
    return [], {"search_index": {}, "fetched_numbers": set(), "last_fetch": "", "airport_names": {}}, "none"


@dataclass(slots=True)
class BuildInputs:
    """Everything reconcile needs (also used by tests / the idempotency check)."""
    plan: PlanData
    groups: dict
    gaps: dict
    vamsys: Any
    index: Any
    evidence: dict
    airport_names: dict
    source: str
    last_fetch: str = ""


def load_inputs(cfg: "Config", log: LogFn = print) -> BuildInputs:
    from . import caac
    from .evidence import build_all_evidence
    from .fr24_obs import build_observations
    from .vamsys import load_vamsys

    plan = caac.load_plan(cfg)
    groups = caac.group_legs(plan.legs, dict(cfg.airport_alias))
    gaps = caac.detect_rotation_gaps(plan.legs, cfg.number_prefix)
    log(f"计划：{len(plan.rows)} 行 / {len(plan.legs)} 航段 / {len(groups)} 组 / 去回程缺失段 {len(gaps)}")
    vam = load_vamsys(cfg)
    log(f"VAMSYS：rev {vam.revision}，航线 {len(vam.routes)}（商业 {len(vam.commercial_routes)}）")
    rows, extras, source = _load_fr24_rows(cfg, log)
    cao = {l.number for l in plan.legs if cfg.is_cargo_op(l.op)}
    cao |= {cfg.callsign_to_number(v.callsign) for v in vam.routes if cfg.is_cargo_op(v.op)}
    index = build_observations(cfg, rows, cao_numbers=cao, iata_to_icao=extras.get("iata_to_icao"),
                               search_index=extras.get("search_index"),
                               fetched_numbers=extras.get("fetched_numbers") or ())
    n_season = sum(1 for o in index.obs if o.window == "season")
    log(f"FR24 观测：{len(index.obs)} 条（本季 {n_season}，最新实飞 {ut.ts_utc_str(index.latest_ts)}），剔除 {index.dropped}")
    keys = set(groups) | {v.key for v in vam.commercial_routes}
    evidence = build_all_evidence(sorted(keys), index, cfg)
    return BuildInputs(plan=plan, groups=groups, gaps=gaps, vamsys=vam, index=index, evidence=evidence,
                       airport_names=dict(extras.get("airport_names") or {}), source=source,
                       last_fetch=str(extras.get("last_fetch") or ""))


def run_reconcile(cfg: "Config", inp: BuildInputs, *, vamsys=None, now: int | None = None) -> BuildResult:
    res = reconcile(cfg, inp.plan, inp.groups, inp.gaps, vamsys or inp.vamsys, inp.evidence, inp.index, now=now,
                    airport_names=inp.airport_names)
    res.meta.fr24_last_fetch = inp.last_fetch
    if inp.source != "store":
        res.meta.warnings.append(f"FR24 数据源为 {inp.source}（fr24_cache.db 不可用）")
    return res


def idempotency_check(cfg: "Config", inp: BuildInputs, result: BuildResult, *, now: int | None = None
                      ) -> tuple[Counter, BuildResult]:
    """Plan 8.2: apply ``result`` to the VAMSYS snapshot in memory and rebuild; returns (remaining effective
    changes by category, second result). Empty Counter = idempotent."""
    from .vamsys import VAirport

    vam = inp.vamsys
    routes = apply_decisions(vam.routes, result.decisions)
    airports = dict(vam.airports)
    for d in result.decisions:
        if d.category == C.AIRPORT_ADD:
            airports[d.target.orig] = VAirport(code=d.target.orig, icao=d.target.orig, iata="",
                                               name=d.details.get("name", ""))
    vam2 = dataclasses.replace(vam, routes=routes, airports=airports)
    res2 = run_reconcile(cfg, inp, vamsys=vam2, now=now)
    return effective_changes(res2.decisions), res2


def build(cfg: "Config", *, run_dir: Path | None = None, log: LogFn = print, write_targets: bool = True) -> Path:
    """CLI entry for ``vcca_plan.py build``: run the full flow, write <run_dir>/decisions.json, return run_dir.

    Fetch tiers always go to <run_dir>/target_states.json; with ``write_targets`` (default) and the store as data
    source they are also upserted into fr24_cache.db (flight targets only: priority/last_state/reason; the store
    keeps last_ok_at via coalesce, and existing last_ok_at is passed through as well). Reg targets are never touched."""
    from .models import dump_decisions

    inp = load_inputs(cfg, log)
    res = run_reconcile(cfg, inp)
    errs = check_closure(res.decisions, inp.vamsys)
    if errs:
        for e in errs[:30]:
            log("去向核账错误：" + e)
        raise RuntimeError(f"去向核账不平（{len(errs)} 处），未写出 decisions.json")
    log(f"去向核账通过：{len(inp.vamsys.commercial_routes)} 条商业航线各有一个去向")
    run_dir = Path(run_dir) if run_dir else cfg.new_run_dir(create=True)
    path = dump_decisions(run_dir / "decisions.json", res.meta, res.decisions)
    log("决策：" + ", ".join(f"{k}={v}" for k, v in res.meta.counts.items()))
    log(f"需人工复核：{sum(1 for d in res.decisions if d.needs_review)}；阈值命中：{res.meta.threshold_hits}")
    (run_dir / "target_states.json").write_text(
        json.dumps([{"query": t.query, "kind": str(t.kind), "priority": str(t.priority),
                     "last_state": t.last_state, "reason": t.reason} for t in res.target_states],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    n_p1 = sum(1 for t in res.target_states if t.priority == Priority.P1)
    if write_targets and inp.source == "store":
        try:
            from .fr24_store import FR24Store
            with FR24Store.open(cfg) as st:
                merged = []
                for t in res.target_states:
                    old = st.get_target(t.query, t.kind)
                    merged.append(dataclasses.replace(t, last_ok_at=old.last_ok_at if old else None))
                n = st.upsert_targets(merged)
            log(f"已回写抓取分级 {n} 条（P1={n_p1}，保留 last_ok_at）")
        except Exception as e:  # noqa: BLE001
            log(f"回写抓取分级失败（忽略）：{e}")
    else:
        log(f"抓取分级 {len(res.target_states)} 条（P1={n_p1}）写入 target_states.json（未回写 SQLite）")
    log(f"decisions.json -> {path}")
    return run_dir
