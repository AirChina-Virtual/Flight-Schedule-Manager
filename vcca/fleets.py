"""Fleet selection and audit (plan 5.7; step 5).

Ports legacy ``compute_recommended_fleets`` / ``eval_vamsys_fleets`` using cfg.fleet_rules + cfg.airport_rules
(both loaded from rules/*.toml). Differences vs legacy (by plan):
  * unrated airport: NOT silently 4C — pool still chosen with the 4C fallback but the result carries
    ReasonCode.UNRATED_AIRPORT and needs_review=True;
  * exclusions emptying the pool -> fall back to narrow pool minus exclusions + FLEET_EXCLUSION_FALLBACK (review).

Rules recap:
  CAO / CCD / BJN (cfg.fleet_rules.fixed_fleets): fixed set; grade warnings only. A cargo route of any operator
  uses the cargo operator's fixed fleets (legacy: ``op == "CAO" or route_type == "cargo"``).
  CCA body tier = plan FD2 ∪ FR24 aircraft (FD19 ignored); none -> VAMSYS current fleets; none -> narrow + review.
  min grade of both ends: NB 3C -> cca_narrowbody['3C']; route_allows_b752 -> ['4D'] (adds B752); else ['4C'].
  WB 4D -> cca_widebody['4D'] (B762/B763 only), 4E -> ['4E'], 4F -> ['4F']; WB below 4D -> narrow pool + review.
  Then subtract airport_rules.route_fleet_exclusions(orig, dest).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from .models import BodyTier, DecisionCategory, Reason, ReasonCode

if TYPE_CHECKING:
    from .config import Config
    from .vamsys import VamsysSnapshot
    from .models import Decision


@dataclass(slots=True)
class FleetRecommendation:
    fleet_ids: frozenset[str]
    body_tier: BodyTier
    grade_orig: str
    grade_dest: str
    grade_min: str
    unrated_airports: tuple[str, ...] = ()
    excluded: frozenset[str] = frozenset()     # removed by airport exclusions
    reasons: list[Reason] = field(default_factory=list)
    needs_review: bool = False
    review_flags: list[str] = field(default_factory=list)

    @property
    def grade_label(self) -> str:
        return f"{self.grade_orig}->{self.grade_dest}(取{self.grade_min})"

    def _add(self, code: str, review: bool = False, **params) -> None:
        self.reasons.append(Reason(str(code), params))
        if review:
            self.needs_review = True
            if str(code) not in self.review_flags:
                self.review_flags.append(str(code))


@dataclass(slots=True)
class FleetAudit:
    """Comparison of a VAMSYS route's fleets with the recommendation."""
    extra: frozenset[str] = frozenset()        # in VAMSYS, not recommended (over-grade / cross-body)
    missing: frozenset[str] = frozenset()
    dirty: frozenset[str] = frozenset()        # fleet_rules.dirty present (DH8D)
    air_force: frozenset[str] = frozenset()
    reasons: list[Reason] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.extra or self.missing or self.dirty or self.air_force)


def _sorted(fids: Iterable[str]) -> list[str]:
    return sorted(fids)


def body_tier_from_codes(cfg: "Config", op: str, plan_fd2: Iterable[str], fr24_fleet_ids: Iterable[str],
                         vamsys_fleet_ids: Iterable[str] = ()) -> tuple[BodyTier, list[Reason], bool]:
    """(tier, reasons, needs_review) for CCA-style operators."""
    fr = cfg.fleet_rules
    nb_all, wb_all = fr.nb_all, fr.wb_all
    fd2s = {str(c or "").strip().upper() for c in plan_fd2 if str(c or "").strip()}
    mapped = {c: fr.map_ac_to_fleet("CCA", c, cargo=False) for c in fd2s}
    has_nb = any(mapped[c] in nb_all for c in fd2s)
    has_wb = any((mapped[c] in wb_all) or c in fr.widebody_fd2_extra for c in fd2s)
    fr_ids = set(fr24_fleet_ids)
    has_nb = has_nb or bool(fr_ids & nb_all)
    has_wb = has_wb or bool(fr_ids & wb_all)
    reasons: list[Reason] = []
    review = False
    if not has_nb and not has_wb:
        v = set(vamsys_fleet_ids) - fr.excluded
        if v:
            has_nb, has_wb = bool(v & nb_all), bool(v & wb_all)
            if has_nb or has_wb:
                reasons.append(Reason(ReasonCode.FLEET_FROM_VAMSYS, {}))
    if not has_nb and not has_wb:
        has_nb = True
        reasons.append(Reason(ReasonCode.FLEET_NO_TYPE_INFO, {}))
        review = True
    tier = BodyTier.MIXED if (has_nb and has_wb) else (BodyTier.WIDE if has_wb else BodyTier.NARROW)
    return tier, reasons, review


def recommend_fleets(cfg: "Config", op: str, orig: str, dest: str, *, route_type: str,
                     plan_fd2: Iterable[str] = (), fr24_fleet_ids: Iterable[str] = (),
                     vamsys_fleet_ids: Iterable[str] = ()) -> FleetRecommendation:
    """Recommended VAMSYS fleet IDs for a route (airports in VAMSYS codes)."""
    ar, fr = cfg.airport_rules, cfg.fleet_rules
    g_o, g_d, g_min, unrated = ar.route_min_grade(orig, dest)
    rank = ar.grade_rank

    def finish(rec: FleetRecommendation) -> FleetRecommendation:
        if unrated:
            rec._add(ReasonCode.UNRATED_AIRPORT, review=True, airports=list(unrated))
        return rec

    cargo_op = cfg.cargo_ops[0] if cfg.cargo_ops else "CAO"
    fixed_op = cargo_op if (cfg.is_cargo_op(op) or route_type == "cargo") else (op if op in fr.fixed_fleets else None)
    if fixed_op is not None and fixed_op in fr.fixed_fleets:
        is_cargo = cfg.is_cargo_op(fixed_op)
        rec = FleetRecommendation(fleet_ids=frozenset(fr.fixed_fleets[fixed_op]),
                                  body_tier=BodyTier.CARGO if is_cargo else BodyTier.FIXED,
                                  grade_orig=g_o, grade_dest=g_d, grade_min=g_min, unrated_airports=unrated)
        if is_cargo and rank[g_min] < rank["4E"]:
            rec._add(ReasonCode.FLEET_GRADE_WARNING, op=fixed_op, grade=rec.grade_label)
        elif not is_cargo and rank[g_min] < rank["4C"]:
            rec._add(ReasonCode.FLEET_GRADE_WARNING, op=fixed_op, grade=rec.grade_label)
        return finish(rec)

    tier, reasons, review = body_tier_from_codes(cfg, op, plan_fd2, fr24_fleet_ids, vamsys_fleet_ids)
    rec = FleetRecommendation(fleet_ids=frozenset(), body_tier=tier, grade_orig=g_o, grade_dest=g_d,
                              grade_min=g_min, unrated_airports=unrated)
    for r in reasons:
        rec._add(r.code, review=(review and r.code == ReasonCode.FLEET_NO_TYPE_INFO), **r.params)

    if g_min == "3C":
        nb_pool = fr.cca_narrowbody["3C"]
    elif ar.route_allows_b752(orig, dest):
        nb_pool = fr.cca_narrowbody["4D"]
    else:
        nb_pool = fr.cca_narrowbody["4C"]
    wb_pool = fr.cca_widebody[g_min] if g_min in ("4D", "4E", "4F") else frozenset()

    if tier == BodyTier.NARROW:
        fids = nb_pool
    elif tier == BodyTier.WIDE:
        if wb_pool:
            fids = wb_pool
            if g_min == "4D":
                rec._add(ReasonCode.FLEET_WB_4D_ONLY, grade=rec.grade_label)
        else:
            fids = nb_pool
            rec._add(ReasonCode.FLEET_WB_DOWNGRADED, review=True, grade=rec.grade_label)
    else:  # MIXED
        if wb_pool:
            fids = nb_pool | wb_pool
            if g_min == "4D":
                rec._add(ReasonCode.FLEET_WB_4D_ONLY, grade=rec.grade_label)
        else:
            fids = nb_pool
            rec._add(ReasonCode.FLEET_WB_DOWNGRADED, grade=rec.grade_label)

    excl = ar.route_fleet_exclusions(orig, dest)
    if excl:
        rec.excluded = frozenset(fids & excl)
        fids = fids - excl
        if not fids:
            fids = nb_pool - excl
            rec._add(ReasonCode.FLEET_EXCLUSION_FALLBACK, review=True, excluded=_sorted(excl))
    rec.fleet_ids = frozenset(fids)
    return finish(rec)


def audit_fleets(cfg: "Config", current: Iterable[str], rec: FleetRecommendation) -> FleetAudit:
    """VAMSYS fleets vs recommendation (dirty / air force / extra / missing)."""
    fr = cfg.fleet_rules
    cur = frozenset(current)
    clean = cur - fr.excluded
    a = FleetAudit(extra=frozenset(clean - rec.fleet_ids), missing=frozenset(rec.fleet_ids - clean),
                   dirty=frozenset(cur & fr.dirty), air_force=frozenset(cur & fr.air_force))
    if a.dirty:
        a.reasons.append(Reason(ReasonCode.FLEET_DIRTY, {"fleets": _sorted(a.dirty)}))
    if a.air_force:
        a.reasons.append(Reason(ReasonCode.FLEET_AIR_FORCE, {"fleets": _sorted(a.air_force)}))
    if a.extra:
        a.reasons.append(Reason(ReasonCode.FLEET_EXTRA, {"fleets": _sorted(a.extra)}))
    if a.missing:
        a.reasons.append(Reason(ReasonCode.FLEET_MISSING, {"tier": str(rec.body_tier), "grade": rec.grade_label,
                                                           "fleets": _sorted(a.missing)}))
    return a


def fleet_usage(cfg: "Config", vamsys: "VamsysSnapshot", decisions: Iterable["Decision"]) -> list[dict]:
    """xlsx sheet 8: per VAMSYS fleet (all 37) -> {'fleet_id','type_code','kind','aircraft','routes_now',
    'routes_after','in_rules': bool, 'note'}."""
    fr = cfg.fleet_rules
    ac_cnt = Counter(a.fleet_id for a in vamsys.aircraft)
    now = Counter(fid for r in vamsys.routes for fid in r.fleet_ids)
    # routes after applying decisions: special routes unchanged + every non-retired decision target
    after: Counter = Counter(fid for r in vamsys.special_routes for fid in r.fleet_ids)
    retire = {DecisionCategory.RETIRE, DecisionCategory.RETIRE_REDUNDANT, DecisionCategory.RETIRE_DEFERRED}
    report_only = {DecisionCategory.WATCH_PLAN, DecisionCategory.CHARTER, DecisionCategory.POSITIONING,
                   DecisionCategory.AIRPORT_ADD}
    for d in decisions:
        if d.category in retire or d.category in report_only:
            continue
        for fid in d.target.fleet_ids:
            after[fid] += 1
    in_rules_all = (set().union(*fr.cca_narrowbody.values()) | set().union(*fr.cca_widebody.values())
                    | {f for v in fr.fixed_fleets.values() for f in v})
    out = []
    for fid, f in sorted(vamsys.fleets.items(), key=lambda kv: (kv[1].type_code, kv[0])):
        if fid in fr.dirty:
            note = "异常机队（按规则剔除）"
        elif fid in fr.air_force:
            note = "空军特勤（不入商业航线）"
        elif fid in in_rules_all:
            note = ""
        else:
            note = "未纳入规则机队池"
        out.append({"fleet_id": fid, "type_code": f.type_code, "kind": f.kind, "aircraft": ac_cnt.get(fid, 0),
                    "routes_now": now.get(fid, 0), "routes_after": after.get(fid, 0),
                    "in_rules": fid in in_rules_all, "note": note})
    return out
