"""Core data structures passed between pipeline stages (plan section 3).

Rules:
  * Stages communicate ONLY through these types; Chinese text is produced by the output layer
    (``describe_reason``), never parsed back for decisions.
  * Weekday sets are ``frozenset[int]`` (0=Mon). Times are 'HH:MM' UTC unless the field name says bjt.
  * Timestamps are int UNIX seconds (UTC).
  * Route key everywhere: ``RouteKey = (op, callsign, orig, dest)`` with airport codes in VAMSYS
    convention (config ``airports.alias`` already applied).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .util_time import (abs_circ_diff, circ_diff, days_to_fd3, days_to_vdays, days_to_zh, parse_hhmm, ts_utc_date,
                        ts_utc_hm)

RouteKey = tuple[str, str, str, str]  # (op, callsign, orig, dest)

DECISIONS_SCHEMA_VERSION = 1


# =====================================================================================================================
# Enums
# =====================================================================================================================
class EvidenceState(StrEnum):
    """Plan 5.3 probe state of one (op, cs, orig, dest); evaluated in this order, first hit wins."""
    ACTIVE = "active"                         # season: this carrier, this leg, actually flown
    SCHED_ONLY = "sched_only"                 # season: only schedules (no actuals)
    STALE_SCHED = "stale_sched"               # sched_only, but every schedule older than the recent window
    CARRIER_CHANGED = "carrier_changed"       # same number+leg flown by another target carrier
    ROUTE_CHANGED = "route_changed"           # same callsign flies other legs
    STOPPED_PRE_SEASON = "stopped_pre_season"  # flown before season start, not since
    NEVER_FLOWN = "never_flown"               # history fetched, 0 actuals in 365 days
    NO_DATA = "no_data"                       # nothing fetched at all (should trend to 0 -> refetch)


class StoppedKind(StrEnum):
    """Sub-kind for STOPPED_PRE_SEASON (plan 5.3: same carrier / other carrier / other route)."""
    SAME_OP = "same_op"
    OTHER_OP = "other_op"
    OTHER_ROUTE = "other_route"


class DecisionCategory(StrEnum):
    """Plan 5.8 decision categories. ``CSV_FILE`` below maps each to its FlightOpsDesk CSV (or None)."""
    CREATE_PLAN = "CREATE_PLAN"
    CREATE_EXTRA_SLOT = "CREATE_EXTRA_SLOT"
    CREATE_FR24 = "CREATE_FR24"
    WATCH_PLAN = "WATCH_PLAN"
    UPDATE = "UPDATE"
    KEEP = "KEEP"
    KEEP_UNPLANNED = "KEEP_UNPLANNED"
    RETIRE = "RETIRE"
    RETIRE_REDUNDANT = "RETIRE_REDUNDANT"
    RETIRE_DEFERRED = "RETIRE_DEFERRED"
    HOLD = "HOLD"
    CHARTER = "CHARTER"
    POSITIONING = "POSITIONING"
    AIRPORT_ADD = "AIRPORT_ADD"


#: category -> CSV stem ("01_airports_add", ...) or None when report-only. Used by export_csv.
CSV_FILE: dict[DecisionCategory, str | None] = {
    DecisionCategory.AIRPORT_ADD: "01_airports_add",
    DecisionCategory.RETIRE: "02_routes_retire",
    DecisionCategory.RETIRE_REDUNDANT: "02_routes_retire",
    DecisionCategory.RETIRE_DEFERRED: "02b_routes_retire_deferred",
    DecisionCategory.UPDATE: "03_routes_update",
    DecisionCategory.CREATE_PLAN: "04_routes_create",
    DecisionCategory.CREATE_EXTRA_SLOT: "04_routes_create",
    DecisionCategory.CREATE_FR24: "04_routes_create",
    DecisionCategory.WATCH_PLAN: None,
    DecisionCategory.KEEP: None,
    DecisionCategory.KEEP_UNPLANNED: None,
    DecisionCategory.HOLD: None,
    DecisionCategory.CHARTER: None,
    DecisionCategory.POSITIONING: None,
}

#: categories that consume an existing VAMSYS route (vroute_id required) -> used by the closure check
#: (every commercial VAMSYS route must appear in exactly one decision of these categories).
VROUTE_CATEGORIES: frozenset[DecisionCategory] = frozenset({
    DecisionCategory.UPDATE, DecisionCategory.KEEP, DecisionCategory.KEEP_UNPLANNED,
    DecisionCategory.RETIRE, DecisionCategory.RETIRE_REDUNDANT, DecisionCategory.RETIRE_DEFERRED,
    DecisionCategory.HOLD,
})

CATEGORY_ZH: dict[DecisionCategory, str] = {
    DecisionCategory.CREATE_PLAN: "新增（计划+FR24）",
    DecisionCategory.CREATE_EXTRA_SLOT: "新增（计划漏列时段）",
    DecisionCategory.CREATE_FR24: "新增（计划外FR24实飞）",
    DecisionCategory.WATCH_PLAN: "待观察（计划有、FR24未飞）",
    DecisionCategory.UPDATE: "调整",
    DecisionCategory.KEEP: "一致",
    DecisionCategory.KEEP_UNPLANNED: "保留（计划外在飞）",
    DecisionCategory.RETIRE: "停用",
    DecisionCategory.RETIRE_REDUNDANT: "停用（冗余时段）",
    DecisionCategory.RETIRE_DEFERRED: "暂缓停用",
    DecisionCategory.HOLD: "保持不动（数据盲区）",
    DecisionCategory.CHARTER: "临时加班/包机",
    DecisionCategory.POSITIONING: "调机/测试",
    DecisionCategory.AIRPORT_ADD: "新增机场",
}


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BodyTier(StrEnum):
    """fleets.py body tier (plan 5.7)."""
    NARROW = "narrow"
    WIDE = "wide"
    MIXED = "mixed"
    CARGO = "cargo"   # CAO fixed fleet
    FIXED = "fixed"   # CCD / BJN fixed fleet


class TargetKind(StrEnum):
    FLIGHT = "flight"   # FR24 flight number query, e.g. "CA1234"
    REG = "reg"         # FR24 registration query, e.g. "B-2447"
    BOARD = "board"     # airport board, e.g. "ZBAA"


class Priority(StrEnum):
    """Plan 6.2 refresh tiers."""
    P0 = "P0"  # never fetched / last failed: fetch now
    P1 = "P1"  # uncertain at last build: TTL ttl_uncertain_h
    P2 = "P2"  # stable: TTL ttl_stable_h


class ReasonCode(StrEnum):
    """Structured reason codes. Params per code are documented in ``REASON_TEXT`` placeholders.
    New codes may be added by any module; add a ``REASON_TEXT`` entry at the same time."""
    # --- plan / FR24 confirmation
    PLAN_FR24_CONFIRMED = "PLAN_FR24_CONFIRMED"
    PLAN_NOT_FLOWN = "PLAN_NOT_FLOWN"
    NO_DATA_REFETCH = "NO_DATA_REFETCH"
    MATCHED_VIA_OLD_OPERATOR = "MATCHED_VIA_OLD_OPERATOR"
    CARRIER_CHANGED = "CARRIER_CHANGED"
    MIXED_CARRIER = "MIXED_CARRIER"
    # --- slot calibration (5.4 / 5.5 / 5.6)
    MICRO_SPLIT_MERGED = "MICRO_SPLIT_MERGED"
    TIME_CALIBRATED = "TIME_CALIBRATED"
    DST_SHIFT = "DST_SHIFT"
    WEEKDAY_ADDED = "WEEKDAY_ADDED"
    WEEKDAY_DROPPED = "WEEKDAY_DROPPED"
    WEEKDAYS_FROM_FR24 = "WEEKDAYS_FROM_FR24"
    BANK_SUPERSEDED = "BANK_SUPERSEDED"
    EXTRA_SLOT = "EXTRA_SLOT"
    SINGLE_DAY_PROMOTED = "SINGLE_DAY_PROMOTED"
    SINGLE_DAY_IGNORED = "SINGLE_DAY_IGNORED"
    FD5_UTC_SUSPECT = "FD5_UTC_SUSPECT"
    # --- VAMSYS pairing
    FIELD_CHANGED = "FIELD_CHANGED"
    VAMSYS_MATCHES_BANK = "VAMSYS_MATCHES_BANK"
    VAMSYS_REDUNDANT = "VAMSYS_REDUNDANT"
    END_DATE_CLEARED = "END_DATE_CLEARED"
    ALREADY_RETIRING = "ALREADY_RETIRING"
    # --- retire / hold / keep
    RETIRE_CARRIER_CHANGED = "RETIRE_CARRIER_CHANGED"
    RETIRE_ROUTE_CHANGED = "RETIRE_ROUTE_CHANGED"
    RETIRE_STOPPED_PRE_SEASON = "RETIRE_STOPPED_PRE_SEASON"
    RETIRE_NEVER_FLOWN = "RETIRE_NEVER_FLOWN"
    RETIRE_STALE_SCHED = "RETIRE_STALE_SCHED"
    RETIRE_OCCASIONAL_OLD_CARRIER = "RETIRE_OCCASIONAL_OLD_CARRIER"
    DEFERRED_PLAN_NOT_FLOWN = "DEFERRED_PLAN_NOT_FLOWN"
    HOLD_ROTATION_GAP_BLIND = "HOLD_ROTATION_GAP_BLIND"
    UNPLANNED_ACTIVE = "UNPLANNED_ACTIVE"
    # --- FR24-only
    FR24_ONLY_ROUTE = "FR24_ONLY_ROUTE"
    ROTATION_GAP_LEG = "ROTATION_GAP_LEG"
    CHARTER_IRREGULAR = "CHARTER_IRREGULAR"
    POSITIONING_FLIGHT = "POSITIONING_FLIGHT"
    # --- fleets (5.7)
    FLEET_EXTRA = "FLEET_EXTRA"
    FLEET_MISSING = "FLEET_MISSING"
    FLEET_DIRTY = "FLEET_DIRTY"
    FLEET_AIR_FORCE = "FLEET_AIR_FORCE"
    FLEET_WB_4D_ONLY = "FLEET_WB_4D_ONLY"
    FLEET_WB_DOWNGRADED = "FLEET_WB_DOWNGRADED"
    FLEET_NO_TYPE_INFO = "FLEET_NO_TYPE_INFO"
    FLEET_FROM_VAMSYS = "FLEET_FROM_VAMSYS"
    FLEET_EXCLUSION_FALLBACK = "FLEET_EXCLUSION_FALLBACK"
    FLEET_GRADE_WARNING = "FLEET_GRADE_WARNING"
    UNRATED_AIRPORT = "UNRATED_AIRPORT"
    # --- airports
    AIRPORT_MISSING = "AIRPORT_MISSING"
    DEPENDS_ON_AIRPORT_ADD = "DEPENDS_ON_AIRPORT_ADD"
    # --- appended (step 5)
    PLAN_SLOT_NOT_FLOWN = "PLAN_SLOT_NOT_FLOWN"
    PLAN_FR24_SCHEDULED = "PLAN_FR24_SCHEDULED"
    NOT_RECENT = "NOT_RECENT"
    MIXED_OP_MINORITY = "MIXED_OP_MINORITY"


#: Chinese templates for ``describe_reason`` (str.format with Reason.params; missing params render as '?').
REASON_TEXT: dict[str, str] = {
    ReasonCode.PLAN_FR24_CONFIRMED: "计划+FR24双确认（实飞{acts}班）",
    ReasonCode.PLAN_NOT_FLOWN: "计划有但FR24未飞（{state}）",
    ReasonCode.NO_DATA_REFETCH: "未抓到FR24数据，需补抓",
    ReasonCode.MATCHED_VIA_OLD_OPERATOR: "按原承运键{old_callsign}匹配到VAMSYS",
    ReasonCode.CARRIER_CHANGED: "FR24换季由{new_callsign}执飞（原表写{old_callsign}）",
    ReasonCode.MIXED_CARRIER: "混飞：主{op}（{acts}班）+他司{others}",
    ReasonCode.MICRO_SPLIT_MERGED: "已合并计划微拆班（{n}子段→1班次）",
    ReasonCode.TIME_CALIBRATED: "按FR24实飞校准时刻（原计划{plan_dep}-{plan_arr}，差{diff}分钟）",
    ReasonCode.DST_SHIFT: "整小时偏差{diff}分钟（夏令时），按近期FR24校准",
    ReasonCode.WEEKDAY_ADDED: "按FR24实飞增补星期（{old}→{new}）",
    ReasonCode.WEEKDAY_DROPPED: "冬季观测{weeks}周该星期0次，删除星期（{old}→{new}，需复核）",
    ReasonCode.WEEKDAYS_FROM_FR24: "按FR24实飞重算星期（{old}→{new}）",
    ReasonCode.BANK_SUPERSEDED: "FR24换季中途调时，旧时段{old}已停用不另建",
    ReasonCode.EXTRA_SLOT: "FR24实飞发现计划漏列独立时段（实飞{acts}，观测{obs}）",
    ReasonCode.SINGLE_DAY_PROMOTED: "单日行（FD23=FD24）冬季实飞{acts}班，升格为在飞航线",
    ReasonCode.SINGLE_DAY_IGNORED: "单日行（FD23=FD24）忽略：{why}",
    ReasonCode.FD5_UTC_SUSPECT: "源表FD5疑似直接写UTC（{fd5}）而FD6写北京时间，已按FR24核准",
    ReasonCode.FIELD_CHANGED: "{field}：{old} → {new}",
    ReasonCode.VAMSYS_MATCHES_BANK: "现行航线吻合FR24时段{bank}",
    ReasonCode.VAMSYS_REDUNDANT: "现行航线未配上任何FR24时段（冗余）",
    ReasonCode.END_DATE_CLEARED: "VAMSYS已设End Date（{end_date}）但仍需运行，清空End Date",
    ReasonCode.ALREADY_RETIRING: "VAMSYS已设End Date（{end_date}），不重复停用",
    ReasonCode.RETIRE_CARRIER_CHANGED: "换承运：冬季由{new_callsign}执飞",
    ReasonCode.RETIRE_ROUTE_CHANGED: "换航段：冬季该呼号飞{other_ods}",
    ReasonCode.RETIRE_STOPPED_PRE_SEASON: "季前有实飞、冬季无（末次{last_date}，{kind}）",
    ReasonCode.RETIRE_NEVER_FLOWN: "近{days}天0实飞",
    ReasonCode.RETIRE_STALE_SCHED: "旧排班消失（末次排班{last_date}）",
    ReasonCode.RETIRE_OCCASIONAL_OLD_CARRIER: "旧承运偶发代飞（{acts}班），不视为在飞",
    ReasonCode.DEFERRED_PLAN_NOT_FLOWN: "计划仍列但FR24未飞（{state}），暂缓停用",
    ReasonCode.HOLD_ROTATION_GAP_BLIND: "去回程缺失段且FR24无观测（数据盲区），不动",
    ReasonCode.UNPLANNED_ACTIVE: "计划无、FR24在飞（{state}），保留并按FR24校准",
    ReasonCode.FR24_ONLY_ROUTE: "计划与VAMSYS均无，FR24实飞{acts}班、跨{weeks}周",
    ReasonCode.ROTATION_GAP_LEG: "去回程缺失段（{out_callsign}/{ret_callsign}）",
    ReasonCode.CHARTER_IRREGULAR: "仅出现{days}天或星期不规律（{dates}）",
    ReasonCode.POSITIONING_FLIGHT: "调机/测试（{why}）",
    ReasonCode.FLEET_EXTRA: "含超飞行区等级或跨宽窄机型（{fleets}）",
    ReasonCode.FLEET_MISSING: "按[{tier}+{grade}]需补齐（{fleets}）",
    ReasonCode.FLEET_DIRTY: "误含异常机队（{fleets}）",
    ReasonCode.FLEET_AIR_FORCE: "误含空军机型（{fleets}）",
    ReasonCode.FLEET_WB_4D_ONLY: "飞行区等级{grade}为4D（宽体仅配B762/B763）",
    ReasonCode.FLEET_WB_DOWNGRADED: "飞行区等级{grade}低于4D（宽体受限，已降为窄体池）",
    ReasonCode.FLEET_NO_TYPE_INFO: "缺计划FD2与FR24机型，暂按窄体",
    ReasonCode.FLEET_FROM_VAMSYS: "缺计划FD2与FR24机型，参考VAMSYS现行机队",
    ReasonCode.FLEET_EXCLUSION_FALLBACK: "机场排除后机队为空，回退窄体池（排除{excluded}）",
    ReasonCode.FLEET_GRADE_WARNING: "注意：{op}机型与飞行区等级{grade}不符",
    ReasonCode.UNRATED_AIRPORT: "机场{airports}未定级，暂按4C窄体池",
    ReasonCode.AIRPORT_MISSING: "VAMSYS缺场{airports}",
    ReasonCode.DEPENDS_ON_AIRPORT_ADD: "依赖先导入机场{airports}",
    ReasonCode.PLAN_SLOT_NOT_FLOWN: "计划时段{dep}-{arr}在FR24冬季{weeks}周观测中无对应时段，暂不新建",
    ReasonCode.PLAN_FR24_SCHEDULED: "计划+FR24排班确认（仅排班{obs}条，尚无实飞）",
    ReasonCode.NOT_RECENT: "FR24末次实飞{last_date}，近{days}天无实飞，暂不新建",
    ReasonCode.MIXED_OP_MINORITY: "混飞少数承运：{callsign}实飞{acts}班（近期{recent}），主承运{main}近期{main_recent}班，不新建",
}


# =====================================================================================================================
# Plan side
# =====================================================================================================================
@dataclass(slots=True)
class PlanLeg:
    """One leg of one CAAC pre-plan row (a row yields 1 or 2 legs). Airport codes are as written in the plan."""
    op: str
    callsign: str              # 'CCA1109'
    number: str                # 'CA1109'
    orig: str
    dest: str
    leg_no: int                # 1 = FD4->FD7, 2 = FD7->FD10
    is_multileg: bool          # row has a second leg (FD10 present)
    bjt_days: str              # raw FD3 operating-day mask of the ROW, e.g. '1.3.5.7'
    day_offset: int            # cumulative BJT day offset of this leg's departure vs. the operating day (leg 1: 0)
    bjt_dep: str               # 'HH:MM' as written (plan tz)
    bjt_arr: str
    utc_dep: str               # 'HH:MM' UTC
    utc_arr: str
    utc_dep_min: int
    utc_arr_min: int
    utc_days: frozenset[int]   # UTC departure weekdays
    fd2: str                   # planned aircraft code (upper)
    fd19: str                  # "或使用机型..." boilerplate (not used for decisions)
    fd26: str                  # service type; 'F' = cargo
    fd23: str                  # 'MMDD' effective from (zero-padded)
    fd24: str                  # 'MMDD' effective to
    single_day: bool           # FD23 == FD24 (legacy: "expired" 1025-1025 rows)
    route_type: str            # 'scheduled' | 'cargo'
    plan_group: str            # '国内' | '国际' | '港澳' (season.plan_groups)
    src_file: str              # file name
    src_row: int               # 1-based sheet row number (header = 1)
    src_ref: str               # legacy-compatible 'file.xls:row' ('#2' suffix for leg 2)

    @property
    def bjt_days_label(self) -> str:
        """Legacy display: '1.3.5.7' or '1.3.5.7(+1d)' for a leg departing on a later day."""
        return f"{self.bjt_days}(+{self.day_offset}d)" if self.day_offset else self.bjt_days

    @property
    def raw_key(self) -> RouteKey:
        return (self.op, self.callsign, self.orig, self.dest)

    @property
    def utc_fd3(self) -> str:
        return days_to_fd3(self.utc_days)


@dataclass(slots=True)
class PlanSlot:
    """Plan legs of one group merged by identical UTC (dep, arr); days = union of their UTC weekdays.
    This is the *raw* plan slot, before any FR24 calibration (calibration lives in reconcile)."""
    dep: str
    arr: str
    days: frozenset[int]
    bjt: str                   # display: '<bjt_days_label> HH:MM-HH:MM' of the last contributing leg (legacy)
    fd2: str
    fd19: str
    src_refs: tuple[str, ...]  # contributing PlanLeg.src_ref
    single_day: bool = False   # built from single-day (FD23==FD24) legs

    @property
    def fd3(self) -> str:
        return days_to_fd3(self.days)

    @property
    def vdays(self) -> str:
        return days_to_vdays(self.days)


@dataclass(slots=True)
class PlanGroup:
    """All plan legs of one route key. Key airports are VAMSYS codes (alias applied); raw_* keep plan codes."""
    key: RouteKey
    legs: list[PlanLeg]
    raw_slots: list[PlanSlot]          # from regular legs; from single-day legs when there are no regular legs
    route_type: str
    plan_group: str
    raw_orig: str
    raw_dest: str

    @property
    def op(self) -> str:
        return self.key[0]

    @property
    def callsign(self) -> str:
        return self.key[1]

    @property
    def orig(self) -> str:
        return self.key[2]

    @property
    def dest(self) -> str:
        return self.key[3]

    @property
    def number(self) -> str:
        return self.legs[0].number

    @property
    def regular_legs(self) -> list[PlanLeg]:
        return [l for l in self.legs if not l.single_day]

    @property
    def single_day_legs(self) -> list[PlanLeg]:
        return [l for l in self.legs if l.single_day]

    @property
    def is_single_day_only(self) -> bool:
        """True when only FD23==FD24 rows exist for this key (plan 5.6 promotion candidate)."""
        return all(l.single_day for l in self.legs)

    @property
    def fd2_codes(self) -> frozenset[str]:
        return frozenset(l.fd2 for l in self.legs if l.fd2)


@dataclass(slots=True, frozen=True)
class RotationGap:
    """Plan 5.1: odd outbound N ends at X, return N+1 starts at Y != X -> foreign leg X->Y missing from the plan.
    Detection only; the leg is created solely from FR24 observations (CREATE_FR24 / HOLD)."""
    op: str
    out_callsign: str
    ret_callsign: str
    orig: str                  # X (outbound end), plan code
    dest: str                  # Y (return start), plan code
    route_type: str

    @property
    def key(self) -> RouteKey:
        return (self.op, self.out_callsign, self.orig, self.dest)


@dataclass(slots=True)
class PlanData:
    """Result of ``caac.load_plan``."""
    rows: list[dict]                   # raw target-operator row dicts (FD1..), legacy raw_rows
    legs: list[PlanLeg]
    files: dict[str, int]              # file name -> target rows
    fd23_mode: str = ""
    fd24_mode: str = ""

    @property
    def numbers(self) -> frozenset[str]:
        return frozenset(l.number for l in self.legs)


# =====================================================================================================================
# VAMSYS side
# =====================================================================================================================
@dataclass(slots=True)
class VRoute:
    """One VAMSYS route (resource 4). Strings are stripped/upper-cased; ``fields`` keeps the raw Fields dict."""
    id: str
    op: str
    callsign: str
    number: str
    orig: str
    dest: str
    type: str                          # 'scheduled' | 'cargo' | 'training' | 'repositioning' | ...
    dep: str                           # 'HH:MM' UTC or ''
    arr: str
    dep_min: int | None
    arr_min: int | None
    days: frozenset[int]
    fleet_ids: frozenset[str]
    start_date: str                    # 'YYYY-MM-DD HH:MM:SS' or ''
    end_date: str
    cost_index: str                    # as stored ('' when unset)
    hidden: bool
    tags: str
    is_special: bool                   # training / repositioning / non-target op / MIL or TRAINING tag
    special_reason: str = ""
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> RouteKey:
        return (self.op, self.callsign, self.orig, self.dest)

    @property
    def has_end_date(self) -> bool:
        return bool(self.end_date.strip())

    @property
    def fd3(self) -> str:
        return days_to_fd3(self.days)

    @property
    def vdays(self) -> str:
        return days_to_vdays(self.days)


# =====================================================================================================================
# FR24 side
# =====================================================================================================================
@dataclass(slots=True)
class Obs:
    """One unified FR24 observation (fr24_obs.py output). Airports already VAMSYS-normalised."""
    fr24_id: str | None
    op: str                    # attributed target operator
    op_source: str             # attribution path: 'callsign' | 'number_alt' | 'cargo_fallback' | 'search_resolved'
                               # | 'airline_fallback' | 'live'
    callsign: str
    number: str                # '' when FR24 publishes no flight number (e.g. CAO continuation legs)
    reg: str
    orig: str
    dest: str
    ac: str                    # FR24 aircraft code, upper
    sched_dep: int | None
    sched_arr: int | None
    real_dep: int | None
    real_arr: int | None
    is_actual: bool
    ev_ts: int                 # real_dep or sched_dep or real_arr or sched_arr
    source: str                # 'flight:CA1234' | 'reg:B-2447' | 'board:ZBAA' | 'live'
    window: str = "season"     # 'pre' (< season start) | 'season' (>= season start)
    is_recent: bool = False    # ev_ts >= latest data ts - recent_window_days
    is_current_sched: bool = False  # legacy is_live_or_fut_sched: actual, status 'scheduled', or >= recent cutoff

    @property
    def key(self) -> RouteKey:
        return (self.op, self.callsign, self.orig, self.dest)


@dataclass(slots=True)
class Bank:
    """A cluster of FR24 times for one route key (plan 5.4)."""
    mode_dep: str
    mode_arr: str
    days: frozenset[int] = frozenset()
    act_days: frozenset[int] = frozenset()
    day_acts: dict[int, int] = field(default_factory=dict)        # weekday -> actual count
    day_weeks: dict[int, list[int]] = field(default_factory=dict)  # weekday -> sorted distinct week indexes seen
    acts: int = 0
    obs: int = 0
    recent_obs: int = 0
    recent_wd_obs: dict[int, int] = field(default_factory=dict)
    first_ts: int | None = None
    last_ts: int | None = None
    ts_list: list[int] = field(default_factory=list, repr=False)
    superseded: list[dict] = field(default_factory=list)  # [{'mode_dep','mode_arr','last_ts'}] banks this one replaced
    act_ts: list[int] = field(default_factory=list, repr=False)   # departure ts of the ACTUAL samples (sorted)

    @property
    def label(self) -> str:
        return f"{self.mode_dep}-{self.mode_arr}"


@dataclass(slots=True)
class Evidence:
    """Per route key evidence summary (evidence.py output)."""
    key: RouteKey
    state: EvidenceState
    acts: int = 0                       # season actuals for this exact key
    obs: int = 0                        # season observations (actual + scheduled)
    recent_acts: int = 0
    banks: list[Bank] = field(default_factory=list)
    weekday_stats: dict[int, dict] = field(default_factory=dict)  # wd -> {'acts','obs','weeks'(int)}
    season_weeks_observed: int = 0      # distinct weeks with any season observation for this key
    ac_counter: dict[str, int] = field(default_factory=dict)
    fleet_ids: frozenset[str] = frozenset()  # FR24 aircraft mapped to VAMSYS fleets
    last_act: int | None = None
    last_obs: int | None = None
    other_ops: list[tuple[str, str, int]] = field(default_factory=list)   # (op, callsign, n) same number+leg
    other_ods: list[tuple[str, str, int]] = field(default_factory=list)   # (orig, dest, n) same callsign
    stopped_kind: StoppedKind | None = None
    pre_last_act: int | None = None
    info: dict[str, Any] = field(default_factory=dict)
    observations: list[Obs] = field(default_factory=list, repr=False)

    @property
    def is_active(self) -> bool:
        return self.state == EvidenceState.ACTIVE


@dataclass(slots=True)
class TargetState:
    """fr24_cache.db ``targets`` row: one fetch target and its refresh tier (plan 6.2 / 6.4)."""
    query: str
    kind: TargetKind
    priority: Priority = Priority.P0
    last_ok_at: int | None = None
    last_state: str = ""        # EvidenceState value (or '') from the last build
    reason: str = ""            # why it is in this tier (free text / code, logged only)


# =====================================================================================================================
# Decisions
# =====================================================================================================================
@dataclass(slots=True)
class Reason:
    code: str                  # ReasonCode value (str to stay forward compatible)
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": str(self.code), "params": _jsonable(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "Reason":
        return cls(code=d["code"], params=dict(d.get("params") or {}))


@dataclass(slots=True)
class RouteTarget:
    """Full desired (or current) state of one VAMSYS route; mirrors the CSV columns."""
    op: str
    callsign: str
    number: str
    orig: str
    dest: str
    type: str = "scheduled"
    dep: str = ""                      # 'HH:MM' UTC
    arr: str = ""
    days: frozenset[int] = frozenset()
    fleet_ids: tuple[str, ...] = ()    # sorted
    cost_index: int | None = None
    hidden: bool = False
    start_date: str = ""
    end_date: str = ""

    @property
    def key(self) -> RouteKey:
        return (self.op, self.callsign, self.orig, self.dest)

    @property
    def vdays(self) -> str:
        return days_to_vdays(self.days)

    @property
    def fd3(self) -> str:
        return days_to_fd3(self.days)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["days"] = sorted(self.days)
        d["fleet_ids"] = list(self.fleet_ids)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RouteTarget":
        d = dict(d)
        d["days"] = frozenset(d.get("days") or ())
        d["fleet_ids"] = tuple(d.get("fleet_ids") or ())
        return cls(**d)

    @classmethod
    def from_vroute(cls, vr: VRoute) -> "RouteTarget":
        ci = int(vr.cost_index) if vr.cost_index.strip().isdigit() else None
        return cls(op=vr.op, callsign=vr.callsign, number=vr.number, orig=vr.orig, dest=vr.dest, type=vr.type,
                   dep=vr.dep, arr=vr.arr, days=vr.days, fleet_ids=tuple(sorted(vr.fleet_ids)), cost_index=ci,
                   hidden=vr.hidden, start_date=vr.start_date, end_date=vr.end_date)


@dataclass(slots=True)
class FieldChange:
    """One changed field of an UPDATE. ``field`` uses RouteTarget attribute names
    ('dep','arr','days','fleet_ids','cost_index','end_date', ...). Values are JSON-able display values
    (days as sorted int list, fleet_ids as list)."""
    field: str
    old: Any
    new: Any

    def to_dict(self) -> dict:
        return {"field": self.field, "old": _jsonable(self.old), "new": _jsonable(self.new)}

    @classmethod
    def from_dict(cls, d: dict) -> "FieldChange":
        return cls(field=d["field"], old=d.get("old"), new=d.get("new"))


def calendar_entries(ev: Evidence) -> list[str]:
    """EvidenceSummary.cal for one key (see there). Season observations only; one entry per (day, time, kind)."""
    out: set[str] = set()
    for o in ev.observations:
        if o.window != "season":
            continue
        t = o.sched_dep or o.real_dep
        if not t:
            continue
        hm = ts_utc_hm(t)
        t_b = t
        if ev.banks:
            b = min(ev.banks, key=lambda c: abs_circ_diff(parse_hhmm(hm), parse_hhmm(c.mode_dep)))
            t_b = t + circ_diff(parse_hhmm(b.mode_dep), parse_hhmm(hm)) * 60
        out.add(f"{ts_utc_date(t_b)} {hm} {'A' if o.is_actual else 'S'}")
    # a day/time with an actual row needs no separate schedule-only entry
    acts = {e[:-2] for e in out if e.endswith("A")}
    return sorted(e for e in out if e.endswith("A") or e[:-2] not in acts)


@dataclass(slots=True)
class EvidenceSummary:
    """Compact JSON-able evidence attached to a decision (full Evidence objects are not serialised)."""
    state: str = ""
    acts: int = 0
    obs: int = 0
    recent_acts: int = 0
    last_act: str = ""                 # 'YYYY-MM-DD'
    banks: list[dict] = field(default_factory=list)  # [{'dep','arr','fd3','acts','obs','recent_obs'}]
    ac: dict[str, int] = field(default_factory=dict)
    regs: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)   # CHARTER / POSITIONING flight dates
    other_ops: list = field(default_factory=list)
    other_ods: list = field(default_factory=list)
    #: per-day season observations for the report calendar: 'YYYY-MM-DD HH:MM A|S' (UTC date in the frame of the
    #: nearest bank, so a 23:55 sample of a 00:25 bank lands on the next day; HH:MM = scheduled (else real) dep;
    #: A = actually flown, S = schedule only). Sorted; absent in decisions.json written before 2026-11-26.
    cal: list[str] = field(default_factory=list)

    @classmethod
    def from_evidence(cls, ev: Evidence | None) -> "EvidenceSummary":
        if ev is None:
            return cls()
        return cls(
            state=str(ev.state), acts=ev.acts, obs=ev.obs, recent_acts=ev.recent_acts,
            last_act=ts_utc_date(ev.last_act) if ev.last_act else "",
            banks=[{"dep": b.mode_dep, "arr": b.mode_arr, "fd3": days_to_fd3(b.days), "acts": b.acts,
                    "obs": b.obs, "recent_obs": b.recent_obs} for b in ev.banks],
            ac=dict(ev.ac_counter), other_ops=[list(x) for x in ev.other_ops],
            other_ods=[list(x) for x in ev.other_ods],
            cal=calendar_entries(ev),
        )

    def to_dict(self) -> dict:
        return _jsonable(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, d: dict | None) -> "EvidenceSummary":
        return cls(**(d or {}))


@dataclass(slots=True)
class Decision:
    """One output row of reconcile (plan 3 / 5.8).

    * ``vroute_id`` set  <=> category in VROUTE_CATEGORIES (the decision consumes that VAMSYS route).
    * ``target`` is the desired state (for RETIRE*: the current state with end_date = effective time;
      for AIRPORT_ADD: op/callsign/number = '', orig = ICAO, dest = '' and ``details['name']``).
    * ``current`` is the VAMSYS state before the change (None for creates / report-only rows).
    * ``changes`` lists only differing fields (UPDATE); CSV export writes only rows with changes.
    """
    category: DecisionCategory
    target: RouteTarget
    vroute_id: str | None = None
    current: RouteTarget | None = None
    changes: list[FieldChange] = field(default_factory=list)
    reasons: list[Reason] = field(default_factory=list)
    evidence: EvidenceSummary = field(default_factory=EvidenceSummary)
    confidence: Confidence = Confidence.MEDIUM
    needs_review: bool = False
    review_flags: list[str] = field(default_factory=list)   # ReasonCode values that triggered review
    group_key: RouteKey | None = None                       # plan group key (if any)
    plan_refs: list[str] = field(default_factory=list)      # PlanLeg.src_ref
    depends_on_airports: list[str] = field(default_factory=list)
    slot_index: str = ""                                    # e.g. '1/2' within its group
    details: dict[str, Any] = field(default_factory=dict)   # category-specific extras (JSON-able)

    @property
    def csv_file(self) -> str | None:
        return CSV_FILE[self.category]

    def add_reason(self, code: str, review: bool = False, **params) -> "Decision":
        self.reasons.append(Reason(str(code), params))
        if review:
            self.needs_review = True
            if str(code) not in self.review_flags:
                self.review_flags.append(str(code))
        return self

    def to_dict(self) -> dict:
        return {
            "category": str(self.category),
            "vroute_id": self.vroute_id,
            "target": self.target.to_dict(),
            "current": self.current.to_dict() if self.current else None,
            "changes": [c.to_dict() for c in self.changes],
            "reasons": [r.to_dict() for r in self.reasons],
            "evidence": self.evidence.to_dict(),
            "confidence": str(self.confidence),
            "needs_review": self.needs_review,
            "review_flags": list(self.review_flags),
            "group_key": list(self.group_key) if self.group_key else None,
            "plan_refs": list(self.plan_refs),
            "depends_on_airports": list(self.depends_on_airports),
            "slot_index": self.slot_index,
            "details": _jsonable(self.details),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        return cls(
            category=DecisionCategory(d["category"]),
            vroute_id=d.get("vroute_id"),
            target=RouteTarget.from_dict(d["target"]),
            current=RouteTarget.from_dict(d["current"]) if d.get("current") else None,
            changes=[FieldChange.from_dict(x) for x in d.get("changes") or []],
            reasons=[Reason.from_dict(x) for x in d.get("reasons") or []],
            evidence=EvidenceSummary.from_dict(d.get("evidence")),
            confidence=Confidence(d.get("confidence") or "medium"),
            needs_review=bool(d.get("needs_review")),
            review_flags=list(d.get("review_flags") or []),
            group_key=tuple(d["group_key"]) if d.get("group_key") else None,
            plan_refs=list(d.get("plan_refs") or []),
            depends_on_airports=list(d.get("depends_on_airports") or []),
            slot_index=d.get("slot_index") or "",
            details=dict(d.get("details") or {}),
        )


@dataclass(slots=True)
class RunMeta:
    """Header of decisions.json: provenance of one build."""
    season: str                        # folder name, e.g. '2026-27-1'
    season_label: str
    generated_at: str                  # 'YYYY-MM-DD HH:MM:SS' UTC
    effective_time: str                # End/Start Date written to CSVs
    season_start: str                  # 'YYYY-MM-DD'
    season_end: str
    plan_rows: int = 0
    plan_legs: int = 0
    plan_numbers: int = 0
    vamsys_revision: int | None = None
    vamsys_storage_revision: int | None = None
    vamsys_routes: int = 0
    vamsys_commercial_routes: int = 0
    fr24_rows: int = 0
    fr24_latest_ts: int | None = None
    fr24_last_fetch: str = ""
    thresholds: dict[str, Any] = field(default_factory=dict)
    threshold_hits: dict[str, int] = field(default_factory=dict)   # threshold name -> routes affected
    counts: dict[str, int] = field(default_factory=dict)           # category -> count
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _jsonable(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> "RunMeta":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# =====================================================================================================================
# decisions.json I/O
# =====================================================================================================================
def dump_decisions(path: str | Path, meta: RunMeta, decisions: Iterable[Decision]) -> Path:
    """Write decisions.json: ``{"schema_version": 1, "meta": {...}, "decisions": [...]}`` (UTF-8, indent 1)."""
    path = Path(path)
    payload = {
        "schema_version": DECISIONS_SCHEMA_VERSION,
        "meta": meta.to_dict(),
        "decisions": [d.to_dict() for d in decisions],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def load_decisions(path: str | Path) -> tuple[RunMeta, list[Decision]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    ver = payload.get("schema_version")
    if ver != DECISIONS_SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported decisions schema_version {ver!r} (expected {DECISIONS_SCHEMA_VERSION})")
    return RunMeta.from_dict(payload["meta"]), [Decision.from_dict(d) for d in payload["decisions"]]


# =====================================================================================================================
# Text (output layer only)
# =====================================================================================================================
class _Missing(dict):
    def __missing__(self, k):
        return "?"


_WEEKDAY_PARAM_KEYS = {"old", "new", "days", "plan_days", "fr24_days", "added", "dropped", "from_days", "to_days"}


def _is_weekday_list(v: Any) -> bool:
    return (isinstance(v, (list, tuple, set, frozenset)) and all(isinstance(x, int) and not isinstance(x, bool)
                                                                and 0 <= x <= 6 for x in v))


def _fmt_param(v: Any, key: str | None = None) -> str:
    if isinstance(v, (set, frozenset)) and all(isinstance(x, int) for x in v):
        return days_to_zh(v) or "-"
    if key is not None and (key in _WEEKDAY_PARAM_KEYS or "days" in key) and _is_weekday_list(v):
        # decisions.json round-trips weekday sets as plain int lists
        return days_to_zh(frozenset(v)) or "-"
    if isinstance(v, (list, tuple, set, frozenset)):
        return ",".join(_fmt_param(x) for x in v)
    if isinstance(v, dict):
        return ",".join(f"{k}:{_fmt_param(x)}" for k, x in v.items())
    return str(v)


def describe_reason(reason: Reason | dict) -> str:
    """Reason -> Chinese sentence for reports/xlsx. Unknown codes render as 'CODE(k=v, ...)'."""
    if isinstance(reason, dict):
        reason = Reason.from_dict(reason)
    tpl = REASON_TEXT.get(reason.code)
    params = {k: _fmt_param(v, k) for k, v in (reason.params or {}).items()}
    if tpl is None:
        inner = ", ".join(f"{k}={v}" for k, v in params.items())
        return f"{reason.code}({inner})" if inner else str(reason.code)
    return tpl.format_map(_Missing(params))


def describe_reasons(reasons: Iterable[Reason], sep: str = " | ") -> str:
    return sep.join(describe_reason(r) for r in reasons)


# =====================================================================================================================
def _jsonable(v: Any) -> Any:
    """Recursively convert sets/tuples/enums/dataclasses/dates to JSON-native values (sets -> sorted lists)."""
    if isinstance(v, StrEnum):
        return str(v)
    if isinstance(v, dict):
        return {str(k) if not isinstance(k, (int, float)) else k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (set, frozenset)):
        try:
            return sorted(_jsonable(x) for x in v)
        except TypeError:
            return [_jsonable(x) for x in v]
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _jsonable(dataclasses.asdict(v))
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, Path):
        return str(v)
    return v
