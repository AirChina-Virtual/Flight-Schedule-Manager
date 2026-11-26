"""FR24 rows -> unified observations (plan 5.2; step 4).

Port of ``load_fr24_unified`` (classify / fn_auth / is_cao_ph / norm_ap) from the former rebuild_vamsys_plan.py, but:
  * input is store rows (fr24_store.FR24Store.iter_rows) — any iterable of FlightRow dicts with an
    extra ``source`` key works, so tests can feed fixtures;
  * CALLSIGN is the primary key (CAO has many records with callsign but no number);
  * airports mapped to VAMSYS codes via ``cfg.airport_alias`` (after IATA->ICAO);
  * window split uses cfg.season_start_ts and the recent window (latest actual ts - recent_window_days).

Attribution priority (5.2), recorded in ``Obs.op_source``:
  1. 'callsign'        record callsign starts with a target op (unless it is a CAO placeholder)
  2. 'number_alt'      alternative flight number field
  3. 'cargo_fallback'  CAO placeholder: cargo aircraft code / known CAO number and callsign CCA... -> CAO
  4. 'search_resolved' same number's season carrier (fn_auth: live/board/history/search resolution)
  5. 'airline_fallback' airline_icao (weakest; ignored for carrier_changed decisions)
  ('live' for live_summary rows: operator = airline_icao, always actual.)
Drop: non-target callsigns (e.g. CA1303 flown as CQH6567), status canceled/diverted, orig == dest,
missing orig/dest/timestamp. Dedup by fr24_id, else (callsign, orig, dest, sched_dep).

Row ``source`` values: 'flight:CA1234' | 'reg:B-2447' | 'board:ZBAA' | 'live' (legacy JSON / store). Rows are
processed in the legacy priority order live > board > flight > reg, so for duplicated FR24 ids the first one wins.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .models import Obs, RouteKey

if TYPE_CHECKING:
    from .config import Config

DROP_STATUSES = ("canceled", "cancelled", "diverted")

_CS_DIGITS_RE = re.compile(r"^[A-Z]{3}0*(\d+)[A-Z]?$")


def identity_check(row: Mapping[str, Any], target_ops: tuple[str, ...], number_prefix: str = "CA") -> str:
    """Flight number vs callsign consistency of one FR24 row.

    FR24 sometimes keeps an aircraft's previous transponder callsign on its next legs (e.g. B-2043 broadcast
    ``CCA1332`` while flying CA1564 / CA1303 / CA1393), or pairs a live track with the wrong flight number. Returns:
      ``"ok"``   - consistent, or nothing to compare (no number / no target callsign);
      ``"fix"``  - number is ours (``CA<digits>``) and the row is schedule-matched (has sched_dep): trust the
                   number, the callsign is stale;
      ``"drop"`` - inconsistent and nothing to trust (no schedule match, or a foreign flight number).
    """
    cs = _u(row.get("callsign"))
    num = _u(row.get("number"))
    if not num or not cs.startswith(target_ops) or num == cs:
        return "ok"
    m_cs = _CS_DIGITS_RE.match(cs)
    if num.startswith(number_prefix) and num[len(number_prefix):].isdigit():
        if m_cs and int(m_cs.group(1)) == int(num[len(number_prefix):]):
            return "ok"
        return "fix" if row.get("sched_dep") else "drop"
    if num.startswith(target_ops):          # e.g. number 'CCD1536' with callsign 'CCD1536' handled above
        m_num = _CS_DIGITS_RE.match(num)
        return "ok" if (m_num and m_cs and int(m_num.group(1)) == int(m_cs.group(1))) else "drop"
    return "drop"                           # another airline's flight number on our callsign
SOURCE_ORDER = {"live": 0, "board": 1, "flight": 2, "reg": 3}


@dataclass(slots=True)
class AttributionContext:
    """Lookups needed by ``attribute_row`` (built once by ``build_attribution_context``)."""
    target_ops: tuple[str, ...]
    number_prefix: str
    cargo_ac_codes: frozenset[str]
    cao_numbers: frozenset[str]                         # plan CAO numbers ∪ VAMSYS CAO numbers
    fn_auth: dict[str, tuple[str, str]] = field(default_factory=dict)   # number -> (op, callsign)
    search_operator: dict[str, str] = field(default_factory=dict)       # number -> operator from search index
    cargo_op: str = "CAO"

    def callsign_for(self, op: str, number: str, fallback: str = "") -> str:
        n = number or ""
        return op + n[len(self.number_prefix):] if n.startswith(self.number_prefix) else (n or fallback)


@dataclass(slots=True)
class ObsIndex:
    """All observations + lookup indexes. Season obs and pre-season obs are both in ``obs`` (see Obs.window)."""
    obs: list[Obs]
    by_key: dict[RouteKey, list[Obs]]                   # (op, callsign, orig, dest), season only
    by_number_od: dict[tuple[str, str, str], list[Obs]]  # (number, orig, dest), season only
    by_callsign: dict[str, list[Obs]]                   # season only
    pre_by_key: dict[RouteKey, list[Obs]]
    pre_by_number_od: dict[tuple[str, str, str], list[Obs]]
    pre_by_callsign: dict[str, list[Obs]]
    fetched_numbers: frozenset[str]                     # numbers with a fetched history (for never_flown vs no_data)
    fetched_callsigns: frozenset[str]
    latest_ts: int | None                               # newest actual ev_ts in the data
    recent_start_ts: int | None                         # latest_ts - recent_window_days
    season_start_ts: int
    ctx: AttributionContext | None = None
    dropped: dict[str, int] = field(default_factory=dict)  # drop reason -> count (report)
    number_prefix: str = "CA"
    search_numbers: frozenset[str] = frozenset()        # numbers/callsigns known only from the search index

    def number_of(self, o: Obs) -> str:
        """Published number, else derived from the callsign (callsign-first matching)."""
        if o.number:
            return o.number
        cs = o.callsign
        return self.number_prefix + cs[3:] if len(cs) > 3 and cs[:3].isalpha() else cs


# ------------------------------------------------------------------ helpers -----------------------------------------
def _u(v: Any) -> str:
    return str(v or "").strip().upper()


def _int(v: Any) -> int | None:
    if v is None or v == "" or v is False:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def row_ev_ts(row: Mapping[str, Any]) -> int | None:
    """real_dep or sched_dep or real_arr or sched_arr (legacy ev_ts); live rows may carry ``ev_ts``/``time``."""
    for k in ("real_dep", "sched_dep", "real_arr", "sched_arr", "ev_ts", "time"):
        t = _int(row.get(k))
        if t:
            return t
    return None


def source_kind(source: str | None) -> str:
    s = str(source or "")
    return s.split(":", 1)[0] if s else ""


def normalize_airport(icao: str | None, iata: str | None, *, alias: Mapping[str, str],
                      iata_to_icao: Mapping[str, str]) -> str | None:
    """Prefer ICAO, else map IATA->ICAO (else keep IATA); then apply VAMSYS alias. None if both empty."""
    code = _u(icao)
    if not code:
        ia = _u(iata)
        if not ia:
            return None
        code = iata_to_icao.get(ia, ia)
    return alias.get(code, code)


def is_actual_row(row: Mapping[str, Any]) -> bool:
    """Legacy row_is_actual: real_dep or real_arr or status_live or status_generic in (landed, departed).
    Implemented (shared by fetch for reg discovery)."""
    return bool(row.get("real_dep") or row.get("real_arr") or row.get("status_live")
                or str(row.get("status_generic") or "").lower() in ("landed", "departed"))


def _has_foreign_callsign(cs: str, target_ops: tuple[str, ...]) -> bool:
    return bool(cs) and len(cs) >= 3 and cs[:3].isalpha() and not cs.startswith(target_ops)


def _search_operator(search_index: Mapping[str, Any] | None) -> dict[str, str]:
    out = {}
    for num, item in ((search_index or {}).get("by_flight") or {}).items():
        op = _u(((item or {}).get("detail") or {}).get("operator"))
        if op:
            out[_u(num)] = op
    return out


def _is_cao_placeholder(ctx: AttributionContext, cs: str, num: str, ac: str, is_act: bool) -> bool:
    """Legacy is_cao_ph: a SCHEDULED row of a cargo flight published under a CCA callsign (or none)."""
    if is_act:
        return False
    is_c = (ac in ctx.cargo_ac_codes) or (num in ctx.cao_numbers) or (ctx.search_operator.get(num) == ctx.cargo_op)
    return is_c and (not cs or cs.startswith("CCA"))


def build_iata_to_icao(rows: Iterable[Mapping[str, Any]], base: Mapping[str, str] | None = None) -> dict[str, str]:
    out = dict(base or {})
    for r in rows:
        for ia, ic in ((r.get("origin_iata"), r.get("origin_icao")), (r.get("dest_iata"), r.get("dest_icao"))):
            ia, ic = _u(ia), _u(ic)
            if ia and ic:
                out.setdefault(ia, ic)
    return out


def build_attribution_context(cfg: "Config", rows: Iterable[Mapping[str, Any]], *, cao_numbers: Iterable[str],
                              search_index: Mapping[str, Any] | None = None) -> AttributionContext:
    """Build fn_auth (number -> (op, callsign)): live rows first (authoritative), then season actual rows, then
    season scheduled rows, then the search index (legacy order)."""
    ops = tuple(cfg.target_ops)
    cargo_op = cfg.cargo_ops[0] if cfg.cargo_ops else "CAO"
    ctx = AttributionContext(target_ops=ops, number_prefix=cfg.number_prefix,
                             cargo_ac_codes=frozenset(cfg.fleet_rules.cargo_ac_codes),
                             cao_numbers=frozenset(_u(n) for n in cao_numbers),
                             search_operator=_search_operator(search_index), cargo_op=cargo_op)
    rows = list(rows)
    season_start = cfg.season_start_ts
    # pass 0: live rows (legacy assigns, i.e. overrides)
    for r in rows:
        if source_kind(r.get("source")) != "live":
            continue
        cs, num, al = _u(r.get("callsign")), _u(r.get("number")), _u(r.get("airline_icao"))
        if num and cs and al in ops:
            ctx.fn_auth[num] = (al, cs)
    # passes 1/2: actual then scheduled season rows (boards are all in-season)
    for actual_pass in (True, False):
        for r in rows:
            kind = source_kind(r.get("source"))
            if kind == "live":
                continue
            if str(r.get("status_generic") or "").lower() in DROP_STATUSES:
                continue
            ts = row_ev_ts(r)
            if kind != "board" and (not ts or ts < season_start):
                continue
            is_act = is_actual_row(r)
            if is_act != actual_pass:
                continue
            al = _u(r.get("airline_icao"))
            cs, ac = _u(r.get("callsign")), _u(r.get("aircraft_code"))
            num = _u(r.get("number")) or (_u(r["source"].split(":", 1)[1]) if kind == "flight" else "")
            if not num or (al and al not in ops) or _has_foreign_callsign(cs, ops):
                continue
            if _is_cao_placeholder(ctx, cs, num, ac, is_act):
                ctx.fn_auth.setdefault(num, (cargo_op, ctx.callsign_for(cargo_op, num)))
            elif cs.startswith(ops):
                ctx.fn_auth.setdefault(num, (cs[:3], cs))
    # pass 3: search index
    for num, item in ((search_index or {}).get("by_flight") or {}).items():
        det = (item or {}).get("detail") or {}
        op, cs, f_up = _u(det.get("operator")), _u(det.get("callsign")), _u(num)
        if op in ops and f_up not in ctx.fn_auth:
            if (op == cargo_op or f_up in ctx.cao_numbers) and (not cs or cs.startswith("CCA")):
                ctx.fn_auth[f_up] = (cargo_op, ctx.callsign_for(cargo_op, f_up))
            elif cs:
                ctx.fn_auth[f_up] = (op, cs)
            else:
                ctx.fn_auth[f_up] = (op, ctx.callsign_for(op, f_up))
    return ctx


def attribute_row(row: Mapping[str, Any], ctx: AttributionContext, *, default_number: str | None = None
                  ) -> tuple[str, str, str, str] | None:
    """-> (op, callsign, number, op_source) or None when the row is not a target-operator flight."""
    ops = ctx.target_ops
    raw_al = _u(row.get("airline_icao"))
    cs = _u(row.get("callsign"))
    num = _u(row.get("number")) or _u(default_number)
    alt = _u(row.get("number_alt"))
    ac = _u(row.get("aircraft_code"))
    if source_kind(row.get("source")) == "live":
        op = raw_al if raw_al in ops else (cs[:3] if cs.startswith(ops) else "")
        if not op:
            return None
        return op, cs or ctx.callsign_for(op, num), num, "live"
    if raw_al and raw_al not in ops:
        return None
    if _has_foreign_callsign(cs, ops):
        return None
    is_act = is_actual_row(row)
    s_op = ctx.search_operator.get(num, "")
    ph = _is_cao_placeholder(ctx, cs, num, ac, is_act)
    cargo_like = ac in ctx.cargo_ac_codes or num in ctx.cao_numbers or s_op == ctx.cargo_op
    if cs.startswith(ops) and not ph:
        return cs[:3], cs, num, "callsign"
    if alt.startswith(ops) and not (not is_act and alt.startswith("CCA") and cargo_like):
        return alt[:3], alt, num, "number_alt"
    if ph or cargo_like:
        return ctx.cargo_op, ctx.callsign_for(ctx.cargo_op, num, cs), num, "cargo_fallback"
    if num in ctx.fn_auth:
        op, acs = ctx.fn_auth[num]
        return op, acs, num, "search_resolved"
    if raw_al in ops:
        return raw_al, ctx.callsign_for(raw_al, num), num, "airline_fallback"
    return None


def _ordered(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows = list(rows)
    return [r for _, r in sorted(enumerate(rows), key=lambda ir: (SOURCE_ORDER.get(source_kind(ir[1].get("source")), 9),
                                                                    ir[0]))]


def build_observations(cfg: "Config", rows: Iterable[Mapping[str, Any]], *, cao_numbers: Iterable[str],
                       iata_to_icao: Mapping[str, str] | None = None,
                       search_index: Mapping[str, Any] | None = None,
                       fetched_numbers: Iterable[str] = (), fetched_callsigns: Iterable[str] = ()) -> ObsIndex:
    """Rows -> ObsIndex: attribute, filter, dedup, normalise airports, set window/is_recent/is_current_sched."""
    rows = _ordered(rows)
    i2i = build_iata_to_icao(rows, iata_to_icao)
    ctx = build_attribution_context(cfg, rows, cao_numbers=cao_numbers, search_index=search_index)
    alias = cfg.airport_alias
    season_start = cfg.season_start_ts
    dropped: dict[str, int] = defaultdict(int)
    fetched_n = {_u(n) for n in fetched_numbers}
    fetched_c = {_u(c) for c in fetched_callsigns}
    seen_ids: set[str] = set()
    seen_keys: set[tuple] = set()
    obs: list[Obs] = []
    raw_status: list[str] = []
    for r in rows:
        src = str(r.get("source") or "")
        kind = source_kind(src)
        default_num = None
        if kind == "flight":
            default_num = _u(src.split(":", 1)[1])
            fetched_n.add(default_num)
        if str(r.get("status_generic") or "").lower() in DROP_STATUSES:
            dropped["canceled_diverted"] += 1
            continue
        idc = identity_check(r, ctx.target_ops, cfg.number_prefix)
        if idc == "drop":
            dropped["identity_conflict"] += 1
            continue
        if idc == "fix":
            # stale callsign: rebuild it from the (schedule-matched) flight number, keeping the operator prefix
            dropped["identity_fixed(kept)"] += 1
            r = dict(r)
            r["callsign"] = ctx.callsign_for(_u(r.get("callsign"))[:3], _u(r.get("number")))
        res = attribute_row(r, ctx, default_number=default_num)
        if res is None:
            dropped["non_target"] += 1
            continue
        op, cs, num, op_src = res
        orig = normalize_airport(r.get("origin_icao"), r.get("origin_iata"), alias=alias, iata_to_icao=i2i)
        dest = normalize_airport(r.get("dest_icao"), r.get("dest_iata") or r.get("destination_iata"),
                                 alias=alias, iata_to_icao=i2i)
        if not orig or not dest:
            dropped["missing_airport"] += 1
            continue
        if orig == dest:
            dropped["same_airport"] += 1
            continue
        ev_ts = row_ev_ts(r)
        if not ev_ts:
            dropped["missing_ts"] += 1
            continue
        fid = str(r.get("id") or "").strip() or None
        fid_key = None if (fid and fid.startswith("nid:")) else fid   # synthetic store ids are not FR24 ids
        dk = (cs or num, orig, dest, _int(r.get("sched_dep")) or ev_ts)
        if fid_key:
            if fid_key in seen_ids:
                dropped["duplicate"] += 1
                continue
            seen_ids.add(fid_key)
        elif dk in seen_keys:
            dropped["duplicate"] += 1
            continue
        seen_keys.add(dk)
        is_act = True if kind == "live" else is_actual_row(r)
        obs.append(Obs(
            fr24_id=fid, op=op, op_source=op_src, callsign=cs, number=num, reg=_u(r.get("registration")),
            orig=orig, dest=dest, ac=_u(r.get("aircraft_code")),
            sched_dep=_int(r.get("sched_dep")), sched_arr=_int(r.get("sched_arr")),
            real_dep=_int(r.get("real_dep")), real_arr=_int(r.get("real_arr")),
            is_actual=is_act, ev_ts=ev_ts, source=src or "row",
            window="season" if ev_ts >= season_start else "pre",
        ))
        raw_status.append(str(r.get("status_generic") or "").lower())
        if kind == "reg" and cs:
            fetched_c.add(cs)

    latest = max((o.ev_ts for o in obs if o.is_actual), default=None)
    recent_start = latest - cfg.thresholds.recent_window_days * 86400 if latest else None
    for o, st in zip(obs, raw_status):
        o.is_recent = bool(recent_start is not None and o.ev_ts >= recent_start)
        o.is_current_sched = bool(o.is_actual or st == "scheduled"
                                  or (recent_start is not None and o.ev_ts >= recent_start))

    idx = ObsIndex(obs=obs, by_key=defaultdict(list), by_number_od=defaultdict(list), by_callsign=defaultdict(list),
                   pre_by_key=defaultdict(list), pre_by_number_od=defaultdict(list), pre_by_callsign=defaultdict(list),
                   fetched_numbers=frozenset(), fetched_callsigns=frozenset(), latest_ts=latest,
                   recent_start_ts=recent_start, season_start_ts=season_start, ctx=ctx, dropped=dict(dropped),
                   number_prefix=cfg.number_prefix)
    for o in obs:
        n = idx.number_of(o)
        if o.window == "season":
            idx.by_key[o.key].append(o)
            idx.by_number_od[(n, o.orig, o.dest)].append(o)
            if o.callsign:
                idx.by_callsign[o.callsign].append(o)
        else:
            idx.pre_by_key[o.key].append(o)
            idx.pre_by_number_od[(n, o.orig, o.dest)].append(o)
            if o.callsign:
                idx.pre_by_callsign[o.callsign].append(o)
    idx.by_key, idx.by_number_od, idx.by_callsign = dict(idx.by_key), dict(idx.by_number_od), dict(idx.by_callsign)
    idx.pre_by_key, idx.pre_by_number_od, idx.pre_by_callsign = (dict(idx.pre_by_key), dict(idx.pre_by_number_od),
                                                                 dict(idx.pre_by_callsign))
    idx.fetched_numbers = frozenset(fetched_n)
    idx.fetched_callsigns = frozenset(fetched_c)
    si = search_index or {}
    idx.search_numbers = frozenset(_u(k) for k in (si.get("by_flight") or {})) | frozenset(
        _u(k) for k in (si.get("by_callsign") or {}))
    return idx
