"""FlightOpsDesk import CSVs (plan 7.2; step 6).

Files written to the run dir (``<n>`` = data-row count, only non-empty files are written):
  01_airports_add_<n>.csv            AIRPORT_ADD
  02_routes_retire_<n>.csv           RETIRE + RETIRE_REDUNDANT          (End Date = meta.effective_time)
  02b_routes_retire_deferred_<n>.csv RETIRE_DEFERRED
  03_routes_update_<n>.csv           UPDATE with >=1 change (KEEP_UNPLANNED with changes too)
  04_routes_create_<n>.csv           CREATE_PLAN / CREATE_EXTRA_SLOT / CREATE_FR24 (Start Date = effective time)
Decisions with ``needs_review`` go to the parallel ``<stem>_待复核_<n>.csv`` (same columns), never the main file.
Categories whose ``models.CSV_FILE`` entry is None (CHARTER / POSITIONING / WATCH_PLAN / HOLD / KEEP*, and any
category added later without a CSV mapping) are report-only and never exported.
Retire decisions are skipped when VAMSYS already has an End Date (no change; reconcile's ALREADY_RETIRING).

Format (matches F:\\vCCA\\plan\\2026-27-1\\flightopsdesk_csv\\*.csv byte-for-byte in style): UTF-8 without BOM,
',' separator, csv.QUOTE_MINIMAL (so only multi-value Fleet IDs / Service Days are quoted), CRLF line endings
(also after the last row), booleans 'TRUE'/'FALSE', '_delete' always 'FALSE', Fleet IDs sorted ascending joined by
',', Service Days per util_time.days_to_vdays, times 'HH:MM', dates 'YYYY-MM-DD HH:MM:SS'.
Pre-export self check (``validate_rows``): column names, ID exists in VAMSYS (02/02b/03) / empty (04), time format,
weekday spelling, every fleet ID exists in VAMSYS fleets, airports exist (or are in the 01 file).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from .models import CSV_FILE, Decision, DecisionCategory, RunMeta
from .util_time import DAY_NAMES, days_to_vdays

if TYPE_CHECKING:
    from .config import Config
    from .vamsys import VamsysSnapshot

log = logging.getLogger("vcca")

COLUMNS: dict[str, tuple[str, ...]] = {
    "01_airports_add": ("ICAO/IATA", "Name", "_delete"),
    "02_routes_retire": (
        "ID", "Departure Airport (ICAO/IATA)", "Arrival Airport (ICAO/IATA)", "Type", "Callsign", "Flight Number",
        "End Date", "_delete",
    ),
    "02b_routes_retire_deferred": (
        "ID", "Departure Airport (ICAO/IATA)", "Arrival Airport (ICAO/IATA)", "Type", "Callsign", "Flight Number",
        "End Date", "_delete",
    ),
    "03_routes_update": (
        "ID", "Departure Airport (ICAO/IATA)", "Arrival Airport (ICAO/IATA)", "Type", "Callsign", "Flight Number",
        "Fleet IDs", "Departure Time (HH:MM)", "Arrival Time (HH:MM)", "Service Days", "Cost Index", "_delete",
    ),
    "04_routes_create": (
        "ID", "Departure Airport (ICAO/IATA)", "Arrival Airport (ICAO/IATA)", "Type", "Callsign", "Flight Number",
        "Fleet IDs", "Start Date", "Departure Time (HH:MM)", "Arrival Time (HH:MM)", "Service Days", "Cost Index",
        "Is Hidden", "_delete",
    ),
}

#: CSV stem -> categories exported into it (documentation; routing uses models.CSV_FILE)
STEM_CATEGORIES: dict[str, tuple[DecisionCategory, ...]] = {
    "01_airports_add": (DecisionCategory.AIRPORT_ADD,),
    "02_routes_retire": (DecisionCategory.RETIRE, DecisionCategory.RETIRE_REDUNDANT),
    "02b_routes_retire_deferred": (DecisionCategory.RETIRE_DEFERRED,),
    "03_routes_update": (DecisionCategory.UPDATE,),
    "04_routes_create": (DecisionCategory.CREATE_PLAN, DecisionCategory.CREATE_EXTRA_SLOT,
                         DecisionCategory.CREATE_FR24),
}

REVIEW_SUFFIX = "_待复核"
LINE_TERMINATOR = "\r\n"
ENCODING = "utf-8"           # no BOM

AIRPORT_STEM = "01_airports_add"
RETIRE_STEMS = frozenset({"02_routes_retire", "02b_routes_retire_deferred"})
UPDATE_STEM = "03_routes_update"
CREATE_STEM = "04_routes_create"
ROUTE_ID_STEMS = RETIRE_STEMS | {UPDATE_STEM}

#: RouteTarget fields the 03 file can carry; changes to other fields (end_date, hidden, start_date) cannot be
#: expressed by an update CSV and are reported as warnings.
UPDATE_FIELDS = frozenset({"orig", "dest", "type", "callsign", "number", "fleet_ids", "dep", "arr", "days",
                           "cost_index"})

_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DATETIME = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]) (?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")
_FILE_RE = re.compile(r"^(?P<stem>.+?)(?P<review>" + re.escape(REVIEW_SUFFIX) + r")?_(?P<n>\d+)\.csv$")


class CsvValidationError(ValueError):
    """Raised by export_csv when the self check fails; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        head = "\n  ".join(self.errors[:50])
        more = f"\n  ...（共 {len(self.errors)} 条）" if len(self.errors) > 50 else ""
        super().__init__(f"CSV 自检失败（{len(self.errors)} 条问题），未写出 CSV：\n  {head}{more}")


# --------------------------------------------------------------------------------------------- routing ------------
def stem_for(d: Decision) -> str | None:
    """CSV stem a decision belongs to, or None when it is report-only (by models.CSV_FILE)."""
    stem = CSV_FILE.get(d.category)
    if stem is None and d.category == DecisionCategory.KEEP_UNPLANNED and d.changes:
        # plan 5.8 B: KEEP_UNPLANNED calibrated by FR24 -> UPDATE; reconcile normally emits UPDATE itself,
        # but a KEEP_UNPLANNED that still carries changes must not be lost.
        stem = UPDATE_STEM
    return stem


def skip_reason(stem: str, d: Decision) -> str | None:
    """Why a decision routed to ``stem`` is not written (None = written)."""
    if stem in RETIRE_STEMS:
        if d.current is not None and (d.current.end_date or "").strip():
            return "already_retiring"
        if not d.vroute_id:
            return "no_vroute_id"
    elif stem == UPDATE_STEM:
        if not d.changes:
            return "no_changes"
        if not any(c.field in UPDATE_FIELDS for c in d.changes):
            return "no_csv_fields"
        if not d.vroute_id:
            return "no_vroute_id"
    return None


def file_key(stem: str, d: Decision) -> str:
    """'03_routes_update' or '03_routes_update_待复核'."""
    return stem + REVIEW_SUFFIX if d.needs_review else stem


def csv_filename(key: str, n: int) -> str:
    return f"{key}_{n}.csv"


def parse_csv_filename(name: str) -> tuple[str, bool, int] | None:
    """'03_routes_update_待复核_12.csv' -> ('03_routes_update', True, 12); None when not one of ours."""
    m = _FILE_RE.match(name)
    if not m or m.group("stem") not in COLUMNS:
        return None
    return m.group("stem"), bool(m.group("review")), int(m.group("n"))


# --------------------------------------------------------------------------------------------- rows ---------------
def _fleets(fids: Iterable[str]) -> str:
    return ",".join(sorted({str(f) for f in fids if str(f)}, key=lambda s: (len(s), s)))


def _ci(v) -> str:
    return "" if v is None or str(v).strip() == "" else str(v).strip()


def decision_to_row(stem: str, d: Decision, *, effective_time: str,
                    default_cost_index: Mapping[str, int] | None = None) -> dict[str, str]:
    """One decision -> CSV row dict keyed by COLUMNS[stem].

    03 update: target values, falling back to the current VAMSYS value for any empty target field (never blanks a
    field out); Cost Index is the current VAMSYS value. 04 create: empty ID, Start Date = effective time,
    Cost Index = target.cost_index or the operator default, Is Hidden FALSE.
    """
    t, cur = d.target, d.current
    if stem == AIRPORT_STEM:
        row = {"ICAO/IATA": t.orig, "Name": str((d.details or {}).get("name") or ""), "_delete": "FALSE"}
        return {k: row[k] for k in COLUMNS[stem]}

    def pick(attr: str):
        v = getattr(t, attr)
        if (v is None or v == "" or v == () or v == frozenset()) and cur is not None:
            return getattr(cur, attr)
        return v

    row = {
        "ID": "" if stem == CREATE_STEM else str(d.vroute_id or ""),
        "Departure Airport (ICAO/IATA)": pick("orig"),
        "Arrival Airport (ICAO/IATA)": pick("dest"),
        "Type": pick("type"),
        "Callsign": pick("callsign"),
        "Flight Number": pick("number"),
    }
    if stem in RETIRE_STEMS:
        row["End Date"] = effective_time
    else:
        row["Fleet IDs"] = _fleets(pick("fleet_ids"))
        row["Departure Time (HH:MM)"] = pick("dep")
        row["Arrival Time (HH:MM)"] = pick("arr")
        row["Service Days"] = days_to_vdays(pick("days"))
        if stem == UPDATE_STEM:
            row["Cost Index"] = _ci(cur.cost_index if cur is not None else t.cost_index)
        else:
            ci = t.cost_index
            if ci is None and default_cost_index:
                ci = default_cost_index.get(t.op)
            row["Cost Index"] = _ci(ci)
        if stem == CREATE_STEM:
            row["Start Date"] = effective_time
            row["Is Hidden"] = "FALSE"
    row["_delete"] = "FALSE"
    return {k: str(row.get(k, "") if row.get(k) is not None else "") for k in COLUMNS[stem]}


# --------------------------------------------------------------------------------------------- validation ---------
def validate_rows(stem: str, rows: Sequence[dict[str, str]], vamsys: "VamsysSnapshot",
                  *, airports_added: Iterable[str] = ()) -> list[str]:
    """Self-check (module doc). Returns error strings; export aborts when non-empty."""
    errs: list[str] = []
    cols = COLUMNS.get(stem)
    if cols is None:
        return [f"{stem}: 未知的 CSV 类型"]
    added = {str(a).strip().upper() for a in airports_added}
    by_id = vamsys.route_by_id() if stem in ROUTE_ID_STEMS else {}
    seen_ids: set[str] = set()
    seen_airports: set[str] = set()

    def airport_ok(code: str) -> bool:
        c = (code or "").strip().upper()
        return bool(c) and (vamsys.has_airport(c) or c in added)

    for i, row in enumerate(rows, start=2):  # line 1 = header
        where = f"{stem} 第{i}行" + (f" ID={row.get('ID')}" if row.get("ID") else "") + \
                (f" {row.get('Callsign', '')}".rstrip() if row.get("Callsign") else "")
        if tuple(row.keys()) != cols:
            errs.append(f"{where}: 列名/列序不符（{list(row.keys())}）")
            continue
        if row["_delete"] != "FALSE":
            errs.append(f"{where}: _delete 应为 FALSE（{row['_delete']!r}）")
        if stem == AIRPORT_STEM:
            code = row["ICAO/IATA"].strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{3,4}", code):
                errs.append(f"{where}: 机场代码不合法 {code!r}")
            elif vamsys.has_airport(code):
                errs.append(f"{where}: 机场 {code} 已存在于 VAMSYS")
            if code in seen_airports:
                errs.append(f"{where}: 机场 {code} 重复")
            seen_airports.add(code)
            if not row["Name"].strip():
                errs.append(f"{where}: 机场 {code} 缺名称")
            continue

        rid = row["ID"].strip()
        if stem in ROUTE_ID_STEMS:
            if not rid:
                errs.append(f"{where}: 缺 ID")
            elif rid not in by_id:
                errs.append(f"{where}: ID {rid} 不存在于 VAMSYS")
            elif rid in seen_ids:
                errs.append(f"{where}: ID {rid} 重复")
            seen_ids.add(rid)
        elif rid:
            errs.append(f"{where}: 新建航线 ID 应为空（{rid!r}）")
        for col in ("Departure Airport (ICAO/IATA)", "Arrival Airport (ICAO/IATA)"):
            if not airport_ok(row[col]):
                errs.append(f"{where}: {col} {row[col]!r} 不在 VAMSYS 也不在 01 新增机场中")
        if row["Departure Airport (ICAO/IATA)"] == row["Arrival Airport (ICAO/IATA)"]:
            errs.append(f"{where}: 起降同场 {row['Departure Airport (ICAO/IATA)']}")
        for col in ("Type", "Callsign", "Flight Number"):
            if not row[col].strip():
                errs.append(f"{where}: 缺 {col}")
        for col in ("End Date", "Start Date"):
            if col in row and not _DATETIME.match(row[col]):
                errs.append(f"{where}: {col} 格式应为 YYYY-MM-DD HH:MM:SS（{row[col]!r}）")
        if stem in RETIRE_STEMS:
            continue
        for col in ("Departure Time (HH:MM)", "Arrival Time (HH:MM)"):
            if not _HHMM.match(row[col]):
                errs.append(f"{where}: {col} 格式应为 HH:MM（{row[col]!r}）")
        days = row["Service Days"].split(",") if row["Service Days"] else []
        if not days:
            errs.append(f"{where}: Service Days 为空")
        else:
            bad = [x for x in days if x not in DAY_NAMES]
            if bad:
                errs.append(f"{where}: Service Days 拼写错误 {bad}")
            elif len(set(days)) != len(days) or days != sorted(days, key=DAY_NAMES.index):
                errs.append(f"{where}: Service Days 重复或顺序不对（{row['Service Days']}）")
        fids = row["Fleet IDs"].split(",") if row["Fleet IDs"] else []
        if not fids:
            errs.append(f"{where}: Fleet IDs 为空")
        else:
            missing = [f for f in fids if f not in vamsys.fleets]
            if missing:
                errs.append(f"{where}: 机队 ID 不存在于 VAMSYS {missing}")
            if len(set(fids)) != len(fids):
                errs.append(f"{where}: Fleet IDs 重复")
        if row["Cost Index"] and not row["Cost Index"].isdigit():
            errs.append(f"{where}: Cost Index 应为整数（{row['Cost Index']!r}）")
        if stem == CREATE_STEM and row["Is Hidden"] not in ("TRUE", "FALSE"):
            errs.append(f"{where}: Is Hidden 应为 TRUE/FALSE（{row['Is Hidden']!r}）")
    return errs


# --------------------------------------------------------------------------------------------- planning -----------
@dataclass
class CsvPlan:
    """Pure result of routing decisions into files (nothing written)."""
    files: dict[str, list[dict[str, str]]] = field(default_factory=dict)   # file key -> rows (insertion = order)
    skipped: dict[str, int] = field(default_factory=dict)                   # skip reason -> count
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.files.items() if v}

    @property
    def airports_added(self) -> set[str]:
        out: set[str] = set()
        for key in (AIRPORT_STEM, AIRPORT_STEM + REVIEW_SUFFIX):
            out |= {r["ICAO/IATA"].strip().upper() for r in self.files.get(key, [])}
        return out


def plan_csv(meta: RunMeta, decisions: Sequence[Decision], cfg: "Config | None" = None) -> CsvPlan:
    """Route + convert decisions (no I/O). Order: stems in COLUMNS order, main file before its review twin,
    decisions in decisions.json order within a file."""
    plan = CsvPlan()
    eff = meta.effective_time
    dci = dict(cfg.default_cost_index) if cfg is not None else None
    for stem in COLUMNS:
        plan.files[stem] = []
        plan.files[stem + REVIEW_SUFFIX] = []
    unexpressible: list[str] = []
    for d in decisions:
        stem = stem_for(d)
        if stem is None:
            continue
        if stem not in COLUMNS:
            plan.warnings.append(f"类别 {d.category} 映射到未知 CSV {stem}，已跳过")
            continue
        why = skip_reason(stem, d)
        if why is not None:
            plan.skipped[why] = plan.skipped.get(why, 0) + 1
            if why == "no_csv_fields":
                unexpressible.append(d.vroute_id or "?")
            continue
        if stem == UPDATE_STEM:
            other = sorted({c.field for c in d.changes} - UPDATE_FIELDS)
            if other:
                unexpressible.append(f"{d.vroute_id}({','.join(other)})")
        plan.files[file_key(stem, d)].append(decision_to_row(stem, d, effective_time=eff, default_cost_index=dci))
    if plan.skipped.get("already_retiring"):
        plan.warnings.append(f"{plan.skipped['already_retiring']} 条停用航线 VAMSYS 已设 End Date，未重复写入 02/02b")
    if unexpressible:
        plan.warnings.append(f"{len(unexpressible)} 条调整含 03 文件无法表达的字段（如 End Date / Is Hidden），"
                             f"需在 FlightOpsDesk 手工处理：{', '.join(unexpressible[:20])}"
                             + (" ..." if len(unexpressible) > 20 else ""))
    if plan.skipped.get("no_vroute_id"):
        plan.warnings.append(f"{plan.skipped['no_vroute_id']} 条停用/调整决策缺 VAMSYS ID，已跳过")
    plan.files = {k: v for k, v in plan.files.items() if v}
    return plan


def validate_plan(plan: CsvPlan, vamsys: "VamsysSnapshot") -> list[str]:
    errs: list[str] = []
    added = plan.airports_added
    for key, rows in plan.files.items():
        stem = key[: -len(REVIEW_SUFFIX)] if key.endswith(REVIEW_SUFFIX) else key
        errs += [e.replace(stem, key, 1) for e in validate_rows(stem, rows, vamsys, airports_added=added)]
    # the same route must not appear in two files (e.g. update + retire)
    owner: dict[str, str] = {}
    for key, rows in plan.files.items():
        for r in rows:
            rid = r.get("ID", "")
            if rid:
                if rid in owner and owner[rid] != key:
                    errs.append(f"ID {rid} 同时出现在 {owner[rid]} 与 {key}")
                owner.setdefault(rid, key)
    return errs


def render_csv(stem: str, rows: Sequence[dict[str, str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=",", quoting=csv.QUOTE_MINIMAL, lineterminator=LINE_TERMINATOR)
    w.writerow(COLUMNS[stem])
    for r in rows:
        w.writerow([r[c] for c in COLUMNS[stem]])
    return buf.getvalue()


def remove_old_csvs(run_dir: Path) -> list[Path]:
    """Delete CSVs a previous export of this run wrote (only names matching our own pattern)."""
    removed = []
    if run_dir.is_dir():
        for p in run_dir.glob("*.csv"):
            if parse_csv_filename(p.name):
                p.unlink()
                removed.append(p)
    return removed


def export_csv(run_dir: Path, meta: RunMeta, decisions: Sequence[Decision], cfg: "Config",
               vamsys: "VamsysSnapshot") -> list[Path]:
    """Write the CSV files (only non-empty ones) into ``run_dir``; returns their paths. Uses meta.effective_time.
    Raises CsvValidationError (nothing written) when the self check fails."""
    run_dir = Path(run_dir)
    plan = plan_csv(meta, decisions, cfg)
    errs = validate_plan(plan, vamsys)
    if errs:
        raise CsvValidationError(errs)
    for w in plan.warnings:
        log.warning("CSV: %s", w)
    run_dir.mkdir(parents=True, exist_ok=True)
    remove_old_csvs(run_dir)
    paths: list[Path] = []
    for key, rows in plan.files.items():
        stem = key[: -len(REVIEW_SUFFIX)] if key.endswith(REVIEW_SUFFIX) else key
        p = run_dir / csv_filename(key, len(rows))
        with open(p, "w", encoding=ENCODING, newline="") as f:
            f.write(render_csv(stem, rows))
        paths.append(p)
    return paths


def csv_counts(paths: Iterable[Path]) -> dict[str, int]:
    """{file key: rows} from file names written by export_csv."""
    out: dict[str, int] = {}
    for p in paths:
        parsed = parse_csv_filename(Path(p).name)
        if parsed:
            stem, review, n = parsed
            out[stem + (REVIEW_SUFFIX if review else "")] = n
    return out
