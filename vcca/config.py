"""Season configuration: season.toml + rules/*.toml -> frozen ``Config``.

Usage::

    from vcca.config import load_config
    cfg = load_config()                 # latest season folder under F:\\vCCA\\plan containing season.toml
    cfg = load_config("2026-27-1")      # by folder name (or absolute path)
    cfg.thresholds.bank_merge           # ((45, 60), (20, 75), (65, 20))
    cfg.airport_rules.route_min_grade("ZBAA", "ZUBD")
    cfg.fleet_rules.map_ac_to_fleet("CCA", "B738")
    cfg.effective_time()                # '2026-11-25 16:00:00' (next BJT midnight, UTC)
    cfg.new_run_dir()                   # <season>/output/2026-11-25_1530/ (not created)

Every key has a default in ``DEFAULTS`` (rules files: built-in defaults are empty -> missing rules file is an error).
Validation collects ALL problems and raises one ``ConfigError`` listing them.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import os
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from . import util_time as ut

# ------------------------------------------------------------------ fixed layout -------------------------------------
WORK_DIR = Path(__file__).resolve().parent.parent          # F:\vCCA\plan\fr24_work


def _env_dir(name: str, default: Path) -> Path:
    """Directory from environment variable ``name`` (read at call time), else ``default``."""
    v = os.environ.get(name, "").strip()
    return Path(v).expanduser().resolve() if v else default


# Environment overrides (code and data can live apart, e.g. /opt/vcca + /srv/vcca on a server). The functions read
# the environment at call time; the constants below are the values at import time (kept for compatibility).
def plan_root() -> Path:
    """Folder holding the season folders: $VCCA_PLAN_ROOT, default the parent of fr24_work."""
    return _env_dir("VCCA_PLAN_ROOT", WORK_DIR.parent)


def rules_dir() -> Path:
    """$VCCA_RULES_DIR, default fr24_work/rules."""
    return _env_dir("VCCA_RULES_DIR", WORK_DIR / "rules")


def default_cache_dir() -> Path:
    """$VCCA_CACHE_DIR, default fr24_work/fr24_cache (the env var also wins over [fetch] cache_dir)."""
    return _env_dir("VCCA_CACHE_DIR", WORK_DIR / "fr24_cache")


def state_dir() -> Path:
    """Web app state (users, sessions, jobs, upload temp files): $VCCA_STATE_DIR, default fr24_work/webstate."""
    return _env_dir("VCCA_STATE_DIR", WORK_DIR / "webstate")


PLAN_ROOT = plan_root()                                     # F:\vCCA\plan
RULES_DIR = rules_dir()
DEFAULT_CACHE_DIR = default_cache_dir()
FLIGHTRADARAPI_DIR = WORK_DIR / "FlightRadarAPI" / "python"
SEASON_FILE = "season.toml"
OUTPUT_SUBDIR = "output"
RUN_DIR_FORMAT = "%Y-%m-%d_%H%M"
EFFECTIVE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ConfigError(Exception):
    """Configuration missing/invalid. ``errors`` holds every individual message."""

    def __init__(self, errors: list[str] | str, source: str | Path | None = None):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        head = f"配置错误 ({source})" if source else "配置错误"
        super().__init__(head + ":\n  - " + "\n  - ".join(self.errors))


# ------------------------------------------------------------------ defaults -----------------------------------------
DEFAULTS: dict[str, dict[str, Any]] = {
    "season": {
        "name": "",                       # default: folder name
        "start_date": "",                 # default: mode(FD23) of the plan + year
        "end_date": "",                   # default: mode(FD24)
        "plan_files": [
            "国内航司国内航班计划（三字码）.xls",
            "国内航司国际航班计划（三字码）.xls",
            "国内航司港澳台航班计划（三字码）.xls",
        ],
        "plan_groups": [],                # default: derived from file names (国内/国际/港澳)
        "plan_tz_offset_min": 480,
    },
    "vamsys": {
        "db_path": "../Data/workspaces.db",
        "workspace_id": "",
        "collection": 1,
    },
    "operators": {
        "target": ["CCA", "CAO", "CCD", "BJN"],
        "cargo": ["CAO"],
        "number_prefix": "CA",
    },
    "airports": {
        "alias": {},
        "names": {},
    },
    "export": {
        "effective_time": "next_bjt_midnight",
        "default_cost_index": {},
    },
    "fetch": {
        "rate_per_sec": 2.0,
        "ttl_uncertain_h": 24,
        "ttl_stable_h": 168,
        "deep_scan_every_days": 7,
        "reg_drop_after_days": 30,
        "max_consecutive_failures": 8,
        "cache_dir": "",                  # default: fr24_work/fr24_cache
    },
    "thresholds": {},                     # see Thresholds dataclass defaults
}


Pair = tuple[int, int]


@dataclass(frozen=True)
class Thresholds:
    """Plan 5.9 thresholds (+ the extra knobs used by 5.4/5.5/5.8). Pairs are (dep_min, arr_min)."""
    recent_window_days: int = 21
    time_tolerance_min: int = 15
    bank_merge: tuple[Pair, ...] = ((45, 60), (20, 75), (65, 20))
    plan_attach: Pair = (45, 60)
    plan_calibrate: Pair = (70, 85)
    time_override_min_acts: int = 2
    weekday_add_min_weeks: int = 2
    weekday_drop_min_weeks: int = 4
    weekday_drop_min_coverage: float = 0.75
    fr24_ac_min_share: float = 0.10
    single_day_promote_acts: int = 5
    carrier_change_min_acts: int = 3
    fr24_add_min_acts: int = 3
    fr24_add_min_weeks: int = 2
    bank_supersede_days: int = 7
    slot_pair_day_penalty: int = 200
    vamsys_bank_match: Pair = (35, 45)
    micro_split_merge: Pair = (40, 50)
    extra_slot_separation: Pair = (45, 60)
    extra_slot_min_acts: int = 2
    dst_shift_min: tuple[int, ...] = (55, 60, 65)
    bank_weight_actual: int = 3
    bank_weight_recent: int = 4
    real_time_round_min: int = 5
    never_flown_lookback_days: int = 365
    rotation_gap_add_min_acts: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def within(pair: Pair, dd: int, da: int) -> bool:
        """abs(dep diff) <= pair[0] and abs(arr diff) <= pair[1]."""
        return abs(dd) <= pair[0] and abs(da) <= pair[1]

    def bank_merge_ok(self, dd: int, da: int) -> bool:
        return any(self.within(p, dd, da) for p in self.bank_merge)


@dataclass(frozen=True)
class FetchSettings:
    rate_per_sec: float = 2.0
    ttl_uncertain_h: int = 24
    ttl_stable_h: int = 168
    deep_scan_every_days: int = 7
    reg_drop_after_days: int = 30
    max_consecutive_failures: int = 8


# ------------------------------------------------------------------ rules ------------------------------------------
@dataclass(frozen=True)
class AirportRules:
    """rules/airport_grades.toml. All maps are read-only views."""
    grade_rank: Mapping[str, int]
    airport_grade: Mapping[str, str]
    high_plateau: frozenset[str]
    historic_b757: frozenset[str]
    fleet_exclusions: Mapping[str, frozenset[str]]
    fleet_groups: Mapping[str, frozenset[str]]
    unrated_fallback_grade: str = "4C"
    b752_fleet_id: str = "26090"

    def grade(self, icao: str) -> str | None:
        """Grade or None when the airport is unrated (caller must flag needs_review, plan 5.7)."""
        return self.airport_grade.get(str(icao or "").strip().upper())

    def grade_or_fallback(self, icao: str) -> str:
        return self.grade(icao) or self.unrated_fallback_grade

    def is_rated(self, icao: str) -> bool:
        return self.grade(icao) is not None

    def min_grade(self, *grades: str) -> str:
        return min(grades, key=lambda g: self.grade_rank[g])

    def route_min_grade(self, orig: str, dest: str) -> tuple[str, str, str, tuple[str, ...]]:
        """(grade_orig, grade_dest, min_grade, unrated_airports). Unrated airports use the fallback grade
        (legacy behaviour) and are listed in the 4th element."""
        g1, g2 = self.grade_or_fallback(orig), self.grade_or_fallback(dest)
        unrated = tuple(a for a in (orig, dest) if not self.is_rated(a))
        return g1, g2, self.min_grade(g1, g2), unrated

    def airport_allows_b752(self, icao: str) -> bool:
        ap = str(icao or "").strip().upper()
        if self.b752_fleet_id in self.fleet_exclusions.get(ap, frozenset()):
            return False
        return self.grade_rank[self.grade_or_fallback(ap)] >= self.grade_rank["4D"] or ap in self.historic_b757

    def route_allows_b752(self, orig: str, dest: str) -> bool:
        return self.airport_allows_b752(orig) and self.airport_allows_b752(dest)

    def route_fleet_exclusions(self, orig: str, dest: str) -> frozenset[str]:
        out: set[str] = set()
        for ap in (str(orig or "").strip().upper(), str(dest or "").strip().upper()):
            out |= self.fleet_exclusions.get(ap, frozenset())
        return frozenset(out)


@dataclass(frozen=True)
class FleetRules:
    """rules/fleets.toml. Grade keys: '3C','4C','4D' (narrow) / '4D','4E','4F' (wide); sets are fully expanded."""
    cca_narrowbody: Mapping[str, frozenset[str]]
    cca_widebody: Mapping[str, frozenset[str]]
    fixed_fleets: Mapping[str, tuple[str, ...]]
    dirty: frozenset[str]
    air_force: frozenset[str]
    cargo_ac_codes: frozenset[str]
    widebody_fd2_extra: frozenset[str]
    ac_to_fleet_pax: Mapping[str, str]
    ac_to_fleet_cargo: Mapping[str, str]
    operator_ac_override: Mapping[str, tuple[str, frozenset[str]]]   # op -> (fleet, codes; '*' = all)

    @property
    def nb_all(self) -> frozenset[str]:
        """Legacy NB_SET (= CCA_NB_4D)."""
        return self.cca_narrowbody["4D"]

    @property
    def wb_all(self) -> frozenset[str]:
        """Legacy WB_SET (= CCA_WB_4F)."""
        return self.cca_widebody["4F"]

    @property
    def excluded(self) -> frozenset[str]:
        return self.dirty | self.air_force

    def map_ac_to_fleet(self, op: str, ac_code: str, *, cargo: bool) -> str | None:
        """FR24/CAAC aircraft code -> VAMSYS fleet ID for ``op`` (legacy map_ac_to_fleet).
        ``cargo`` = op is a cargo operator (Config.is_cargo_op); prefer ``Config.map_ac_to_fleet``."""
        ac = str(ac_code or "").upper().strip()
        if cargo:
            return self.ac_to_fleet_cargo.get(ac)
        if op in self.operator_ac_override:
            fid, codes = self.operator_ac_override[op]
            if "*" in codes or ac in codes:
                return fid
        return self.ac_to_fleet_pax.get(ac)


# ------------------------------------------------------------------ config -------------------------------------------
@dataclass(frozen=True)
class Config:
    season_name: str                       # folder name, e.g. '2026-27-1'
    season_label: str                      # season.name, e.g. '2026-2027 冬春'
    season_dir: Path
    season_file: Path
    start_date: _dt.date
    end_date: _dt.date
    dates_source: str                      # 'config' | 'derived' (FD23/FD24 mode)
    plan_files: tuple[Path, ...]
    plan_groups: tuple[str, ...]           # label per plan file
    plan_tz_offset_min: int
    vamsys_db: Path
    workspace_id: str
    collection: int
    target_ops: tuple[str, ...]
    cargo_ops: tuple[str, ...]
    number_prefix: str
    airport_alias: Mapping[str, str]
    airport_names: Mapping[str, str]
    effective_time_mode: str               # 'next_bjt_midnight' or fixed 'YYYY-MM-DD HH:MM:SS'
    default_cost_index: Mapping[str, int]
    fetch: FetchSettings
    thresholds: Thresholds
    airport_rules: AirportRules
    fleet_rules: FleetRules
    cache_dir: Path
    output_root: Path
    warnings: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # ---- derived paths
    @property
    def cache_db(self) -> Path:
        return self.cache_dir / "fr24_cache.db"

    @property
    def season_start_ts(self) -> int:
        """UTC epoch of season start 00:00 plan-local (legacy WINTER_START_TS)."""
        return ut.local_midnight_utc_ts(self.start_date, self.plan_tz_offset_min)

    @property
    def season_end_ts(self) -> int:
        """UTC epoch of the END of the last season day (exclusive)."""
        return ut.local_midnight_utc_ts(self.end_date + _dt.timedelta(days=1), self.plan_tz_offset_min)

    # ---- identifiers
    def callsign_to_number(self, callsign: str) -> str:
        """'CCA1234' -> 'CA1234' (target ops only; otherwise returned unchanged)."""
        cs = str(callsign or "").strip().upper()
        return self.number_prefix + cs[3:] if cs.startswith(self.target_ops) else cs

    def number_to_callsign(self, op: str, number: str) -> str:
        """('CAO', 'CA1234') -> 'CAO1234'."""
        n = str(number or "").strip().upper()
        return op + n[len(self.number_prefix):] if n.startswith(self.number_prefix) else n

    def op_of_callsign(self, callsign: str) -> str | None:
        cs = str(callsign or "").strip().upper()
        return cs[:3] if cs.startswith(self.target_ops) else None

    def vamsys_airport(self, code: str) -> str:
        """Apply airports.alias (FR24/plan code -> VAMSYS code)."""
        c = str(code or "").strip().upper()
        return self.airport_alias.get(c, c)

    def is_cargo_op(self, op: str) -> bool:
        return op in self.cargo_ops

    def map_ac_to_fleet(self, op: str, ac_code: str) -> str | None:
        """FR24/CAAC aircraft code -> VAMSYS fleet ID (cargo ops use [ac_to_fleet_cargo])."""
        return self.fleet_rules.map_ac_to_fleet(op, ac_code, cargo=self.is_cargo_op(op))

    # ---- time
    def effective_time(self, now: _dt.datetime | None = None) -> str:
        """End Date / Start Date for CSVs, 'YYYY-MM-DD HH:MM:SS' UTC.
        'next_bjt_midnight' -> next 00:00 plan-local after ``now`` (BJT: '<date> 16:00:00')."""
        if self.effective_time_mode == "next_bjt_midnight":
            return ut.next_local_midnight_utc(now, self.plan_tz_offset_min).strftime(EFFECTIVE_TIME_FORMAT)
        return self.effective_time_mode

    # ---- runs
    def new_run_dir(self, now: _dt.datetime | None = None, create: bool = False) -> Path:
        """<season>/output/<YYYY-MM-DD_HHMM>/ in UTC (decided 2026-11-26: all report/run times are UTC; folders
        named before that were renamed from BJT to UTC so that name order = time order)."""
        now = now or _dt.datetime.now(ut.UTC)
        p = self.output_root / now.astimezone(ut.UTC).strftime(RUN_DIR_FORMAT)
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def list_runs(self) -> list[Path]:
        """Existing run folders, oldest first."""
        if not self.output_root.is_dir():
            return []
        return sorted(p for p in self.output_root.iterdir()
                      if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}", p.name))

    def latest_run(self, with_decisions: bool = True) -> Path | None:
        runs = [r for r in self.list_runs() if not with_decisions or (r / "decisions.json").is_file()]
        return runs[-1] if runs else None

    def resolve_run(self, name: str | None) -> Path | None:
        """--run argument (folder name or path) -> run dir; None -> latest with decisions.json."""
        if not name:
            return self.latest_run()
        p = Path(name)
        if not p.is_absolute():
            p = self.output_root / name
        return p if p.is_dir() else None


# ------------------------------------------------------------------ loading ------------------------------------------
def discover_seasons(plan_root: Path | None = None) -> list[Path]:
    """Season folders (direct children of plan_root containing season.toml), oldest first.
    Order: season.start_date from the toml when present, else folder name."""
    plan_root = Path(plan_root) if plan_root else globals()["plan_root"]()
    out = []
    for p in plan_root.iterdir() if plan_root.is_dir() else []:
        f = p / SEASON_FILE
        if p.is_dir() and f.is_file():
            sd = ""
            try:
                sd = str(tomllib.loads(f.read_text(encoding="utf-8")).get("season", {}).get("start_date") or "")
            except (tomllib.TOMLDecodeError, OSError):
                pass
            out.append((sd, p.name, p))
    return [p for _, _, p in sorted(out)]


def resolve_season_dir(season: str | Path | None = None, plan_root: Path | None = None) -> Path:
    plan_root = Path(plan_root) if plan_root else globals()["plan_root"]()
    if season:
        p = Path(season)
        if not p.is_absolute():
            p = plan_root / p
        if not (p / SEASON_FILE).is_file():
            raise ConfigError(f"航季文件夹 {p} 中没有 {SEASON_FILE}")
        return p
    seasons = discover_seasons(plan_root)
    if not seasons:
        raise ConfigError(f"{plan_root} 下没有包含 {SEASON_FILE} 的航季文件夹")
    return seasons[-1]


# Unsaved config text to validate without touching the files (web config editor): {resolved path: text}.
_TEXT_OVERRIDES: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("vcca_text_overrides", default={})


@contextlib.contextmanager
def text_overrides(mapping: Mapping[str | Path, str]):
    """Within the block, ``load_config`` reads the given TOML files from ``mapping`` instead of disk."""
    tok = _TEXT_OVERRIDES.set({str(Path(k).resolve()): v for k, v in mapping.items()})
    try:
        yield
    finally:
        _TEXT_OVERRIDES.reset(tok)


def _read_toml(path: Path) -> dict:
    over = _TEXT_OVERRIDES.get()
    try:
        if over and str(Path(path).resolve()) in over:
            return tomllib.loads(over[str(Path(path).resolve())])
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"文件不存在: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML 语法错误: {e}", path) from None


def load_airport_rules(path: Path | None = None) -> AirportRules:
    path = path or rules_dir() / "airport_grades.toml"
    d = _read_toml(path)
    errs = []
    grade_rank = {str(k): int(v) for k, v in d.get("grade_rank", {}).items()}
    if not grade_rank:
        errs.append("[grade_rank] 为空")
    groups = {k: frozenset(map(str, v)) for k, v in d.get("fleet_groups", {}).items()}
    grades = {}
    for ap, g in d.get("airport_grade", {}).items():
        if g not in grade_rank:
            errs.append(f"[airport_grade] {ap} = {g!r} 不在 grade_rank 中")
        grades[ap.upper()] = g
    excl = {}
    for ap, items in d.get("airport_fleet_exclusions", {}).items():
        s: set[str] = set()
        for it in items:
            it = str(it)
            if it.startswith("@"):
                if it[1:] not in groups:
                    errs.append(f"[airport_fleet_exclusions] {ap}: 未定义的组 {it}")
                    continue
                s |= groups[it[1:]]
            else:
                s.add(it)
        excl[ap.upper()] = frozenset(s)
    fb = d.get("meta", {}).get("unrated_fallback_grade", "4C")
    if fb not in grade_rank:
        errs.append(f"[meta] unrated_fallback_grade = {fb!r} 不在 grade_rank 中")
    if errs:
        raise ConfigError(errs, path)
    return AirportRules(
        grade_rank=MappingProxyType(grade_rank),
        airport_grade=MappingProxyType(grades),
        high_plateau=frozenset(d.get("high_plateau", {}).get("airports", [])),
        historic_b757=frozenset(d.get("historic_b757_high_plateau", {}).get("airports", [])),
        fleet_exclusions=MappingProxyType(excl),
        fleet_groups=MappingProxyType(groups),
        unrated_fallback_grade=fb,
    )


def load_fleet_rules(path: Path | None = None) -> FleetRules:
    path = path or rules_dir() / "fleets.toml"
    d = _read_toml(path)
    errs = []
    for sect, keys in (("cca_narrowbody", ("3C", "4C", "4D")), ("cca_widebody", ("4D", "4E", "4F"))):
        for k in keys:
            if k not in d.get(sect, {}):
                errs.append(f"[{sect}] 缺少 {k!r}")
    override = {}
    for op, v in d.get("operator_ac_override", {}).items():
        if "fleet" not in v:
            errs.append(f"[operator_ac_override.{op}] 缺少 fleet")
            continue
        override[op] = (str(v["fleet"]), frozenset(map(str, v.get("ac_codes", []))))
    if errs:
        raise ConfigError(errs, path)
    fz = lambda xs: frozenset(map(str, xs))  # noqa: E731
    return FleetRules(
        cca_narrowbody=MappingProxyType({k: fz(v) for k, v in d["cca_narrowbody"].items()}),
        cca_widebody=MappingProxyType({k: fz(v) for k, v in d["cca_widebody"].items()}),
        fixed_fleets=MappingProxyType({k: tuple(map(str, v)) for k, v in d.get("fixed_fleets", {}).items()}),
        dirty=fz(d.get("excluded", {}).get("dirty", [])),
        air_force=fz(d.get("excluded", {}).get("air_force", [])),
        cargo_ac_codes=fz(d.get("cargo", {}).get("ac_codes", [])),
        widebody_fd2_extra=fz(d.get("cargo", {}).get("widebody_fd2_extra", [])),
        ac_to_fleet_pax=MappingProxyType({str(k): str(v) for k, v in d.get("ac_to_fleet_pax", {}).items()}),
        ac_to_fleet_cargo=MappingProxyType({str(k): str(v) for k, v in d.get("ac_to_fleet_cargo", {}).items()}),
        operator_ac_override=MappingProxyType(override),
    )


def _merge(defaults: dict, user: dict) -> tuple[dict, list[str]]:
    """Shallow-per-section merge; returns (merged, warnings for unknown keys)."""
    warns = []
    out = {}
    for sect, dv in defaults.items():
        uv = user.get(sect, {})
        if not isinstance(uv, dict):
            warns.append(f"[{sect}] 应为表(table)，已忽略")
            uv = {}
        merged = dict(dv)
        merged.update(uv)
        if sect != "thresholds":
            for k in uv:
                if k not in dv:
                    warns.append(f"[{sect}] 未知键 {k!r}（已忽略）")
        out[sect] = merged
    for sect in user:
        if sect not in defaults:
            warns.append(f"未知配置节 [{sect}]（已忽略）")
    return out, warns


def _parse_date(s: str, what: str, errs: list[str]) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(s))
    except ValueError:
        errs.append(f"{what} = {s!r} 不是合法日期 (YYYY-MM-DD)")
        return None


def _build_thresholds(user: dict, errs: list[str], warns: list[str]) -> Thresholds:
    base = Thresholds()
    kw = {}
    for k, v in user.items():
        if not hasattr(base, k):
            warns.append(f"[thresholds] 未知阈值 {k!r}（已忽略）")
            continue
        dv = getattr(base, k)
        try:
            if k == "bank_merge":
                val = tuple((int(a), int(b)) for a, b in v)
                if not val:
                    raise ValueError("empty")
            elif k == "dst_shift_min":
                val = tuple(int(x) for x in v)
            elif isinstance(dv, tuple):
                if len(v) != 2:
                    raise ValueError("need [dep, arr]")
                val = (int(v[0]), int(v[1]))
            elif isinstance(dv, float):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError("need number")
                val = float(v)
                if val > 1:
                    raise ValueError("need ratio 0..1")
            else:
                if isinstance(v, bool) or int(v) != v:
                    raise ValueError("need integer")
                val = int(v)
        except (TypeError, ValueError) as e:
            errs.append(f"[thresholds] {k} = {v!r} 格式错误（{e}；默认 {dv!r}）")
            continue
        flat = [x for p in (val if isinstance(val, tuple) else (val,)) for x in (p if isinstance(p, tuple) else (p,))]
        if any(x < 0 for x in flat):
            errs.append(f"[thresholds] {k} = {v!r} 不能为负数")
            continue
        if k in ("bank_merge", "plan_attach", "plan_calibrate", "vamsys_bank_match", "micro_split_merge",
                 "extra_slot_separation", "time_tolerance_min") and any(x > 720 for x in flat):
            errs.append(f"[thresholds] {k} = {v!r} 超出 0..720 分钟")
            continue
        kw[k] = val
    th = Thresholds(**kw)
    if th.recent_window_days < 1:
        errs.append("[thresholds] recent_window_days 必须 >= 1")
    if th.real_time_round_min < 1:
        errs.append("[thresholds] real_time_round_min 必须 >= 1")
    return th


_GROUP_BY_NAME = (("国际", "国际"), ("港澳台", "港澳"), ("国内航班", "国内"))


def _default_group_label(fname: str) -> str:
    for needle, label in _GROUP_BY_NAME:
        if needle in fname:
            return label
    return Path(fname).stem


def load_config(season: str | Path | None = None, *, strict: bool = True, derive_dates: bool = True,
                rules_dir: Path | None = None, plan_root: Path | None = None) -> Config:
    """Load and validate the season configuration.

    strict=True: missing plan files / VAMSYS DB are errors. strict=False: they become warnings (``status``).
    derive_dates: when start/end are not configured, read the plan files to take mode(FD23)/mode(FD24).
    rules_dir / plan_root default to ``rules_dir()`` / ``plan_root()`` (environment overrides).
    """
    rules_dir = Path(rules_dir) if rules_dir else globals()["rules_dir"]()
    plan_root = Path(plan_root) if plan_root else globals()["plan_root"]()
    season_dir = resolve_season_dir(season, plan_root)
    season_file = season_dir / SEASON_FILE
    user = _read_toml(season_file)
    data, warns = _merge(DEFAULTS, user)
    errs: list[str] = []

    s, v, o, a, ex, fe = (data[k] for k in ("season", "vamsys", "operators", "airports", "export", "fetch"))

    # operators
    target = tuple(str(x).upper() for x in o["target"])
    if not target or any(not re.fullmatch(r"[A-Z]{3}", x) for x in target):
        errs.append(f"[operators] target = {o['target']!r} 必须是非空的三字母 ICAO 航司代码列表")
    cargo = tuple(str(x).upper() for x in o["cargo"])
    if set(cargo) - set(target):
        errs.append(f"[operators] cargo {sorted(set(cargo) - set(target))} 不在 target 中")
    prefix = str(o["number_prefix"]).upper()
    if not re.fullmatch(r"[A-Z0-9]{2}", prefix):
        errs.append(f"[operators] number_prefix = {o['number_prefix']!r} 应为两位 IATA 代码")

    # plan files
    plan_files = tuple(season_dir / f for f in s["plan_files"])
    if not plan_files:
        errs.append("[season] plan_files 为空")
    for p in plan_files:
        if not p.is_file():
            (errs if strict else warns).append(f"计划文件不存在: {p}")
    groups = tuple(s["plan_groups"]) or tuple(_default_group_label(p.name) for p in plan_files)
    if len(groups) != len(plan_files):
        errs.append(f"[season] plan_groups 数量 ({len(groups)}) 与 plan_files ({len(plan_files)}) 不一致")
    tz = s["plan_tz_offset_min"]
    if not isinstance(tz, int) or not -720 <= tz <= 840:
        errs.append(f"[season] plan_tz_offset_min = {tz!r} 应为 -720..840 的整数分钟")
        tz = 480

    # dates
    start = _parse_date(s["start_date"], "[season] start_date", errs) if s["start_date"] else None
    end = _parse_date(s["end_date"], "[season] end_date", errs) if s["end_date"] else None
    dates_source = "config"
    need_derive = (not s["start_date"] or not s["end_date"]) and not (s["start_date"] and start is None)
    if need_derive and derive_dates and all(p.is_file() for p in plan_files) and target:
        from .caac import fd23_fd24_modes  # lazy: caac imports config types only

        try:
            fd23, fd24 = fd23_fd24_modes(plan_files, target)
        except Exception as e:  # noqa: BLE001 - unreadable plan file -> config error, not a crash
            fd23 = fd24 = ""
            errs.append(f"读取计划文件推导航季日期失败: {e}")
        m = re.match(r"(\d{4})", season_dir.name)
        year = int(m.group(1)) if m else _dt.date.today().year
        try:
            if start is None:
                start = _dt.date(year, int(fd23[:2]), int(fd23[2:]))
                dates_source = "derived"
            if end is None:
                cand = _dt.date(start.year, int(fd24[:2]), int(fd24[2:]))
                end = cand if cand > start else _dt.date(start.year + 1, cand.month, cand.day)
                dates_source = "derived"
        except (ValueError, TypeError):
            errs.append(f"无法由 FD23/FD24 众数 ({fd23!r}/{fd24!r}) 推导航季起止日期，请在 [season] 中填写")
    if start is None or end is None:
        if not errs:
            errs.append("[season] start_date / end_date 未填写且无法由计划文件推导")
        start = start or _dt.date(1970, 1, 1)
        end = end or _dt.date(1970, 1, 2)
    elif start >= end:
        errs.append(f"[season] start_date {start} 必须早于 end_date {end}")

    # vamsys
    db = Path(v["db_path"])
    if not db.is_absolute():
        db = (plan_root / db).resolve()
    if not db.is_file():
        (errs if strict else warns).append(f"VAMSYS 数据库不存在: {db}")
    if not re.fullmatch(r"[0-9a-f-]{36}", str(v["workspace_id"])):
        errs.append(f"[vamsys] workspace_id = {v['workspace_id']!r} 不是合法 UUID")
    if not isinstance(v["collection"], int):
        errs.append("[vamsys] collection 应为整数")

    # airports
    alias = {str(k).upper(): str(x).upper() for k, x in dict(a["alias"]).items()}
    names = {str(k).upper(): str(x) for k, x in dict(a["names"]).items()}

    # export
    eff = str(ex["effective_time"]).strip()
    if eff != "next_bjt_midnight":
        try:
            fmt = EFFECTIVE_TIME_FORMAT if eff.count(":") == 2 else "%Y-%m-%d %H:%M"
            eff = _dt.datetime.strptime(eff, fmt).strftime(EFFECTIVE_TIME_FORMAT)
        except ValueError:
            errs.append(f"[export] effective_time = {ex['effective_time']!r} 应为 'next_bjt_midnight' 或 'YYYY-MM-DD HH:MM:SS'")
    ci = {}
    for k, x in dict(ex["default_cost_index"]).items():
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            errs.append(f"[export] default_cost_index.{k} = {x!r} 应为非负整数")
        else:
            ci[str(k).upper()] = x
    missing_ci = [op for op in target if op not in ci]
    if missing_ci:
        errs.append(f"[export] default_cost_index 缺少航司 {missing_ci}")

    # fetch
    fkw = {}
    for f in fields(FetchSettings):
        val = fe[f.name]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
            errs.append(f"[fetch] {f.name} = {val!r} 应为正数")
        else:
            fkw[f.name] = float(val) if f.type in ("float", float) else int(val)
    fetch = FetchSettings(**fkw)
    if os.environ.get("VCCA_CACHE_DIR", "").strip():
        cache_dir = default_cache_dir()
    else:
        cache_dir = Path(fe["cache_dir"]) if fe["cache_dir"] else default_cache_dir()
    if not cache_dir.is_absolute():
        cache_dir = (WORK_DIR / cache_dir).resolve()

    thresholds = _build_thresholds(dict(user.get("thresholds", {})), errs, warns)

    # rules
    try:
        airport_rules = load_airport_rules(rules_dir / "airport_grades.toml")
    except ConfigError as e:
        errs.extend(e.errors)
        airport_rules = None
    try:
        fleet_rules = load_fleet_rules(rules_dir / "fleets.toml")
    except ConfigError as e:
        errs.extend(e.errors)
        fleet_rules = None
    if fleet_rules is not None:
        for op in target:
            if op not in fleet_rules.fixed_fleets and op != "CCA":
                warns.append(f"fleets.toml [fixed_fleets] 未定义 {op}（将按 CCA 规则选机队）")

    if errs:
        raise ConfigError(errs, season_file)

    return Config(
        season_name=season_dir.name,
        season_label=str(s["name"] or season_dir.name),
        season_dir=season_dir,
        season_file=season_file,
        start_date=start,
        end_date=end,
        dates_source=dates_source,
        plan_files=plan_files,
        plan_groups=groups,
        plan_tz_offset_min=tz,
        vamsys_db=db,
        workspace_id=str(v["workspace_id"]),
        collection=int(v["collection"]),
        target_ops=target,
        cargo_ops=cargo,
        number_prefix=prefix,
        airport_alias=MappingProxyType(alias),
        airport_names=MappingProxyType(names),
        effective_time_mode=eff,
        default_cost_index=MappingProxyType(ci),
        fetch=fetch,
        thresholds=thresholds,
        airport_rules=airport_rules,
        fleet_rules=fleet_rules,
        cache_dir=cache_dir,
        output_root=season_dir / OUTPUT_SUBDIR,
        warnings=tuple(warns),
        raw=MappingProxyType(user),
    )


# ------------------------------------------------------------------ environment helpers ------------------------------
def ensure_flightradarapi_on_path() -> Path:
    """Insert fr24_work/FlightRadarAPI/python at the front of sys.path (the venv .pth is stale). Idempotent."""
    p = str(FLIGHTRADARAPI_DIR)
    if not FLIGHTRADARAPI_DIR.is_dir():
        raise ConfigError(f"FlightRadarAPI 库不存在: {FLIGHTRADARAPI_DIR}")
    if p not in sys.path:
        sys.path.insert(0, p)
    return FLIGHTRADARAPI_DIR


CREDENTIAL_VARS = ("FR24_USER", "FR24_PASSWORD")


def fr24_credentials() -> tuple[str | None, str | None]:
    """(user, password) from the environment variables FR24_USER / FR24_PASSWORD (on the server: systemd
    EnvironmentFile /etc/vcca/vcca.env). No .env file search. Callers must never log the values."""
    u, p = os.environ.get("FR24_USER", ""), os.environ.get("FR24_PASSWORD", "")
    return (u, p) if u and p else (None, None)


def credential_status(cfg: Config | None = None) -> dict[str, Any]:  # noqa: ARG001 - cfg kept for callers
    """Whether FR24 credentials are set — presence only, NEVER values.
    Returns {'source': 'ENV'|None, 'checked': [('环境变量 FR24_USER / FR24_PASSWORD', has_user, has_password)]}."""
    hu, hp = bool(os.environ.get("FR24_USER")), bool(os.environ.get("FR24_PASSWORD"))
    return {"source": "ENV" if hu and hp else None,
            "checked": [("环境变量 FR24_USER / FR24_PASSWORD", hu, hp)]}


def count_mode(values: Iterable[str]) -> str:
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else ""
