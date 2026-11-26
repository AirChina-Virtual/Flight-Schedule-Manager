"""Per-route evidence: probe state, time banks, weekday stats, aircraft (plan 5.3 / 5.4; step 4).

Ports legacy ``probe`` (inside load_fr24_unified), ``_extract_fr24_pair_rows``, ``extract_fr24_slots_and_fleets``,
``cluster_fr24_slots``, ``drop_superseded_banks`` and the FD5 check of ``detect_mixed_tz_and_choose_utc``.

State order (5.3, first hit wins) for key (op, cs, orig, dest):
  ACTIVE            season obs for this key with >=1 actual
  SCHED_ONLY        season obs, no actuals; -> STALE_SCHED when every obs is before the recent window
                    (legacy: not is_current_sched)
  CARRIER_CHANGED   no own season obs; same number+leg flown by other target op(s) with
                    >= carrier_change_min_acts recent actuals (op_source != 'airline_fallback')
  ROUTE_CHANGED     same callsign has season obs on other legs
  STOPPED_PRE_SEASON pre-season actuals: same key (SAME_OP), same number+leg other op (OTHER_OP),
                    or same callsign other leg (OTHER_ROUTE)
  NEVER_FLOWN       number/callsign fetched but 0 actuals within never_flown_lookback_days
  NO_DATA           nothing fetched for this number/callsign

Banks (5.4): times = sched (or real rounded to real_time_round_min when no sched in the set); pair weight
bank_weight_actual x actual, bank_weight_recent x recent; merge when cfg.thresholds.bank_merge_ok(dd, da);
supersede rule bank_supersede_days. Banks sorted by (acts, recent_obs, obs) desc.

Differences vs legacy (by plan):
  * carrier change needs >= carrier_change_min_acts RECENT actuals of the other op (legacy: any obs);
    weaker evidence is kept in ``other_ops`` and noted in ``info['threshold_blocked']``;
  * ``Bank.day_weeks`` / ``Evidence.weekday_stats`` / ``season_weeks_observed`` feed the weekday add/drop rules.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

from .models import Bank, Evidence, EvidenceState, Obs, PlanLeg, RouteKey, StoppedKind
from .util_time import (fd3_to_days, hm_circ_diff, parse_hhmm, round_ts, ts_utc_hm, ts_utc_weekday, week_index)

if TYPE_CHECKING:
    from .config import Config, Thresholds
    from .fr24_obs import ObsIndex

FALLBACK_SRC = "airline_fallback"
#: default for threshold ``fr24_ac_min_share``: an FR24 aircraft type counts for the body tier (plan 5.7) only if it
#: flew >= 2 times and >= this share of the key's actuals (one-off substitutions do not flip narrow <-> wide).
FR24_AC_MIN_SHARE = 0.10


@dataclass(slots=True)
class Fd5Check:
    """Result of the FD5-written-as-UTC heuristic for one plan leg (hint only; never auto-applied by caac)."""
    suspect: bool
    utc_dep: str                 # recommended UTC dep ('HH:MM')
    utc_arr: str
    days: frozenset[int]         # recommended UTC weekdays
    dep_diff: int | None         # plan(-8h) vs nearest FR24 bank, minutes (None without FR24 data)
    arr_diff: int | None


def time_pairs(obs: Sequence[Obs], *, round_min: int = 5) -> list[tuple[str, str, int, bool, bool, int]]:
    """(dep_hm, arr_hm, utc_weekday, is_actual, is_recent, dep_ts) per obs; scheduled times when any obs has
    both sched_dep/sched_arr, else real times rounded (legacy _extract_fr24_pair_rows)."""
    rows = [o for o in obs if o.sched_dep and o.sched_arr]
    real = False
    if not rows:
        rows = [o for o in obs if o.real_dep and o.real_arr]
        real = True
    out = []
    for o in rows:
        d = round_ts(o.real_dep, round_min) if real else int(o.sched_dep)
        a = round_ts(o.real_arr, round_min) if real else int(o.sched_arr)
        out.append((ts_utc_hm(d), ts_utc_hm(a), ts_utc_weekday(d), bool(o.is_actual), bool(o.is_recent), int(d)))
    return out


def _absd(a: str, b: str) -> int:
    return abs(hm_circ_diff(a, b))


def cluster_banks(obs: Sequence[Obs], th: "Thresholds", *, week_anchor_ts: int) -> list[Bank]:
    """Cluster season observations of one key into banks (incl. supersede pass). ``week_anchor_ts`` (season
    start) is the reference for Bank.day_weeks week indexes (util_time.week_index)."""
    parsed = time_pairs(obs, round_min=th.real_time_round_min)
    if not parsed:
        return []
    score: Counter = Counter()
    for d, a, _wd, act, rec, _ts in parsed:
        score[(d, a)] += (th.bank_weight_actual if act else 1) * (th.bank_weight_recent if rec else 1)
    banks: list[Bank] = []
    # most_common is stable on ties -> deterministic for identical input order; sort ties by pair for safety
    for (d, a), _w in sorted(score.items(), key=lambda kv: (-kv[1], kv[0])):
        if not any(th.bank_merge_ok(_absd(d, b.mode_dep), _absd(a, b.mode_arr)) for b in banks):
            banks.append(Bank(mode_dep=d, mode_arr=a))
    days = defaultdict(set)
    act_days = defaultdict(set)
    day_weeks = defaultdict(lambda: defaultdict(set))
    for d, a, wd, act, rec, ts in parsed:
        b = min(banks, key=lambda c: _absd(d, c.mode_dep) + _absd(a, c.mode_arr))
        # weekday in the bank's frame: a 23:55 sample of a 00:25 bank belongs to the next UTC day
        ts_b = ts + hm_circ_diff(b.mode_dep, d) * 60
        wd = ts_utc_weekday(ts_b)
        b.obs += 1
        b.first_ts = ts if b.first_ts is None else min(b.first_ts, ts)
        b.last_ts = ts if b.last_ts is None else max(b.last_ts, ts)
        b.ts_list.append(ts)
        days[id(b)].add(wd)
        if act:
            b.acts += 1
            b.act_ts.append(ts_b)
            act_days[id(b)].add(wd)
            b.day_acts[wd] = b.day_acts.get(wd, 0) + 1
            day_weeks[id(b)][wd].add(week_index(ts_b, week_anchor_ts))
        if rec:
            b.recent_obs += 1
            b.recent_wd_obs[wd] = b.recent_wd_obs.get(wd, 0) + 1
    for b in banks:
        b.days = frozenset(days[id(b)])
        b.act_days = frozenset(act_days[id(b)])
        b.day_weeks = {wd: sorted(ws) for wd, ws in sorted(day_weeks[id(b)].items())}
        b.day_acts = dict(sorted(b.day_acts.items()))
        b.recent_wd_obs = dict(sorted(b.recent_wd_obs.items()))
        b.ts_list.sort()
        b.act_ts.sort()
    banks = [b for b in banks if b.obs]
    banks = drop_superseded(banks, th)
    banks.sort(key=lambda c: (c.acts, c.recent_obs, c.obs), reverse=True)
    return banks


def drop_superseded(banks: list[Bank], th: "Thresholds") -> list[Bank]:
    """Remove banks replaced by a mid-season retime; record them in the latest bank's ``superseded``.
    A bank is superseded when its last sample (any, incl. future schedules) is > bank_supersede_days older than
    the latest ACTUAL sample of the newest flown bank, and that bank has >= 2 actual samples after it
    (legacy drop_superseded_banks compared all samples; FR24 now lists schedules days ahead, so a bank of
    never-flown placeholder schedules could "supersede" banks that really flew)."""
    if len(banks) < 2:
        return banks
    flown = [b for b in banks if b.act_ts]
    if not flown:
        return banks
    gap = th.bank_supersede_days * 86400
    latest = max(flown, key=lambda c: (c.act_ts[-1], c.acts))
    last_act = latest.act_ts[-1]
    kept = []
    for c in banks:
        if c is not latest and c.last_ts is not None:
            n_after = sum(1 for t in latest.act_ts if t > c.last_ts)
            if c.last_ts < last_act - gap and n_after >= 2:
                latest.superseded.append({"mode_dep": c.mode_dep, "mode_arr": c.mode_arr, "last_ts": c.last_ts,
                                          "acts": c.acts, "obs": c.obs})
                continue
        kept.append(c)
    return kept


def _number(cfg: "Config", cs: str) -> str:
    return cfg.callsign_to_number(cs)


def build_evidence(key: RouteKey, index: "ObsIndex", cfg: "Config") -> Evidence:
    """Evidence for one key (state per module doc, banks, weekday stats, ac_counter, fleet_ids via
    cfg.map_ac_to_fleet, other_ops/other_ods, observations)."""
    op, cs, orig, dest = key
    th = cfg.thresholds
    num = _number(cfg, cs)
    ctx = index.ctx
    cands = list(index.by_key.get(key, ()))
    if cands and all(o.op_source == FALLBACK_SRC for o in cands) and ctx is not None:
        auth = ctx.fn_auth.get(num)
        if auth and auth[0] != op:
            cands = []
    ev = Evidence(key=key, state=EvidenceState.NO_DATA)
    info = ev.info
    info["threshold_blocked"] = []
    info["recent_start_ts"] = index.recent_start_ts

    # other operators on the same number + leg (season)
    same_num = index.by_number_od.get((num, orig, dest), ())
    other = [o for o in same_num if o.op != op and o.op_source != FALLBACK_SRC]
    oc = Counter((o.op, o.callsign) for o in other if o.is_actual) or Counter((o.op, o.callsign) for o in other)
    ev.other_ops = [(a, b, n) for (a, b), n in sorted(oc.items(), key=lambda kv: (-kv[1], kv[0]))]
    other_recent_acts = Counter((o.op, o.callsign) for o in other if o.is_actual and o.is_recent)
    info["other_recent_acts"] = sum(other_recent_acts.values())
    # same callsign, other legs (season)
    other_legs = [o for o in index.by_callsign.get(cs, ()) if (o.orig, o.dest) != (orig, dest)]
    rc = Counter((o.orig, o.dest) for o in other_legs if o.is_actual) or Counter((o.orig, o.dest) for o in other_legs)
    ev.other_ods = [(a, b, n) for (a, b), n in sorted(rc.items(), key=lambda kv: (-kv[1], kv[0]))]

    if cands:
        acts = [o for o in cands if o.is_actual]
        ev.observations = sorted(cands, key=lambda o: o.ev_ts)
        ev.acts, ev.obs = len(acts), len(cands)
        ev.recent_acts = sum(1 for o in acts if o.is_recent)
        ev.last_act = max((o.ev_ts for o in acts), default=None)
        ev.last_obs = max(o.ev_ts for o in cands)
        ac = Counter(o.ac for o in (acts or cands) if o.ac)
        ev.ac_counter = dict(sorted(ac.items(), key=lambda kv: (-kv[1], kv[0])))
        # body tier evidence: ignore one-off substitutions (type must fly >= 2x and >= fr24_ac_min_share)
        total = sum(ac.values())
        share = float(getattr(th, "fr24_ac_min_share", FR24_AC_MIN_SHARE))
        fr = cfg.fleet_rules

        def body(c):
            f = cfg.map_ac_to_fleet(op, c)
            return "nb" if f in fr.nb_all else "wb" if f in fr.wb_all else (f or c)

        cls = Counter()
        for c, n in ac.items():
            cls[body(c)] += n
        typical = ([c for c, n in ac.items() if cls[body(c)] >= 2 and cls[body(c)] >= share * total]
                   or [c for c, _ in ac.most_common(1)])
        if len(typical) < len(ac):
            info["ac_ignored"] = {c: n for c, n in ac.items() if c not in typical}
        ev.fleet_ids = frozenset(f for f in (cfg.map_ac_to_fleet(op, c) for c in typical) if f)
        ev.banks = cluster_banks(cands, th, week_anchor_ts=index.season_start_ts)
        _weekday_stats(ev, cands, index)
        if acts:
            ev.state = EvidenceState.ACTIVE
        elif any(o.is_current_sched for o in cands):
            ev.state = EvidenceState.SCHED_ONLY
        else:
            ev.state = EvidenceState.STALE_SCHED
            info["last_sched"] = ev.last_obs
        return ev

    if other:
        n_recent = info["other_recent_acts"]
        if n_recent >= th.carrier_change_min_acts:
            ev.state = EvidenceState.CARRIER_CHANGED
            top = other_recent_acts.most_common()
            top.sort(key=lambda kv: (-kv[1], kv[0]))
            info["new_op"], info["new_callsign"] = top[0][0]
            return ev
        info["threshold_blocked"].append("carrier_change_min_acts")

    if other_legs:
        ev.state = EvidenceState.ROUTE_CHANGED
        return ev

    lookback = (index.latest_ts or index.season_start_ts) - th.never_flown_lookback_days * 86400
    pre_same = [o for o in index.pre_by_key.get(key, ()) if o.is_actual and o.ev_ts >= lookback]
    pre_other_op = [o for o in index.pre_by_number_od.get((num, orig, dest), ())
                    if o.op != op and o.op_source != FALLBACK_SRC and o.is_actual and o.ev_ts >= lookback]
    pre_other_rt = [o for o in index.pre_by_callsign.get(cs, ()) if o.is_actual and o.ev_ts >= lookback]
    for kind, lst in ((StoppedKind.SAME_OP, pre_same), (StoppedKind.OTHER_OP, pre_other_op),
                      (StoppedKind.OTHER_ROUTE, pre_other_rt)):
        if lst:
            ev.state = EvidenceState.STOPPED_PRE_SEASON
            ev.stopped_kind = kind
            ev.pre_last_act = max(o.ev_ts for o in lst)
            info["pre_n"] = len(lst)
            if kind == StoppedKind.OTHER_OP:
                info["pre_ops"] = sorted({(o.op, o.callsign) for o in lst})
            elif kind == StoppedKind.OTHER_ROUTE:
                info["pre_ods"] = sorted({(o.orig, o.dest) for o in lst})
            return ev

    if num in index.fetched_numbers or cs in index.fetched_callsigns:
        ev.state = EvidenceState.NEVER_FLOWN
        info["lookback_days"] = th.never_flown_lookback_days
        return ev
    if num in index.search_numbers or cs in index.search_numbers:
        info["search_only"] = True
    ev.state = EvidenceState.NO_DATA
    return ev


def _weekday_stats(ev: Evidence, cands: Sequence[Obs], index: "ObsIndex") -> None:
    """weekday -> {'acts','obs','weeks'} (weeks = distinct weeks with an actual on that weekday);
    season_weeks_observed = distinct weeks (up to the latest data) with any observation of the key."""
    anchor = index.season_start_ts
    latest = index.latest_ts
    stats: dict[int, dict] = {}
    weeks_wd: dict[int, set] = defaultdict(set)
    all_weeks: set[int] = set()
    for o in cands:
        t = o.sched_dep or o.real_dep or o.ev_ts
        wd = ts_utc_weekday(t)
        s = stats.setdefault(wd, {"acts": 0, "obs": 0, "weeks": 0, "recent_acts": 0})
        s["obs"] += 1
        if latest is None or o.ev_ts <= latest:
            all_weeks.add(week_index(o.ev_ts, anchor))
        if o.is_actual:
            s["acts"] += 1
            weeks_wd[wd].add(week_index(t, anchor))
            if o.is_recent:
                s["recent_acts"] += 1
    for wd, s in stats.items():
        s["weeks"] = len(weeks_wd.get(wd, ()))
    ev.info["act_weeks"] = len(set().union(*weeks_wd.values())) if weeks_wd else 0
    ev.weekday_stats = dict(sorted(stats.items()))
    ev.season_weeks_observed = len(all_weeks)


def build_all_evidence(keys: Iterable[RouteKey], index: "ObsIndex", cfg: "Config") -> dict[RouteKey, Evidence]:
    out: dict[RouteKey, Evidence] = {}
    for k in keys:
        k = tuple(k)
        if k not in out:
            out[k] = build_evidence(k, index, cfg)
    return out


def unplanned_keys(index: "ObsIndex", known: Iterable[RouteKey]) -> list[RouteKey]:
    """Season keys observed in FR24 that are neither in the plan nor in VAMSYS (input for 5.8 C)."""
    known = set(map(tuple, known))
    return sorted(k for k in index.by_key if k not in known)


def fd5_utc_check(leg: PlanLeg, evidence: Evidence | None, *, tz_offset_min: int = 480) -> Fd5Check:
    """Legacy heuristic: |plan block - FR24 block| > 120 and FD5-as-UTC within 20 min of the FR24 bank dep and
    arr within 20 -> suspect (dep from FR24, days = unshifted FD3; see caac.fd5_as_utc_alternative)."""
    ud, ua = leg.utc_dep, leg.utc_arr
    banks = evidence.banks if evidence else []
    if not banks:
        return Fd5Check(False, ud, ua, leg.utc_days, None, None)
    best = min(banks, key=lambda b: _absd(ud, b.mode_dep) + _absd(ua, b.mode_arr))
    fr_d, fr_a = best.mode_dep, best.mode_arr
    bjt_dur = (parse_hhmm(leg.bjt_arr) - parse_hhmm(leg.bjt_dep)) % 1440
    fr_dur = (parse_hhmm(fr_a) - parse_hhmm(fr_d)) % 1440
    dep_diff = hm_circ_diff(ud, fr_d)
    arr_diff = hm_circ_diff(ua, fr_a)
    if abs(bjt_dur - fr_dur) > 120 and _absd(leg.bjt_dep, fr_d) <= 20 and abs(arr_diff) <= 20:
        return Fd5Check(True, fr_d, ua, fd3_to_days(leg.bjt_days), 0, arr_diff)
    return Fd5Check(False, ud, ua, leg.utc_days, dep_diff, arr_diff)
