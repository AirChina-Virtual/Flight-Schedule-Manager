"""Multi-sheet review workbook ``重算清单.xlsx`` (plan 7.1; step 6).

Sheets (names fixed; openpyxl):
  "0 总览"          data-source versions (plan rows/legs, VAMSYS revision, FR24 freshness), counts per category,
                    closure check, CSV files, thresholds + threshold_hits (RunMeta), warnings
  "1 新增"          CREATE_PLAN / CREATE_EXTRA_SLOT / CREATE_FR24
  "2 调整"          UPDATE (and KEEP_UNPLANNED with changes): per-field 现行 -> 推荐, reasons, FR24 evidence
  "3 停用"          RETIRE / RETIRE_REDUNDANT / RETIRE_DEFERRED
  "4 计划外临时航班"  CHARTER / POSITIONING (dates, aircraft, registrations, first/last)
  "5 待观察"        WATCH_PLAN / HOLD (and any future category without a sheet of its own)
  "6 一致"          KEEP / KEEP_UNPLANNED without changes
  "7 去回程缺失段"   Decisions with ReasonCode.ROTATION_GAP_LEG / HOLD_ROTATION_GAP_BLIND
  "8 机队与机场审计"  fleets.fleet_usage + unrated airports + missing airports (AIRPORT_ADD)
  "9 人工复核"       every needs_review decision with review_flags
Text comes only from models.describe_reason / CATEGORY_ZH. Days shown as fd3 ('1.3.5.7') and VAMSYS form.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import export_csv as XC
from . import report_summary as RS
from .models import (CATEGORY_ZH, CSV_FILE, Decision, DecisionCategory, ReasonCode, RunMeta)
from .util_time import days_to_fd3, days_to_vdays

if TYPE_CHECKING:
    from .config import Config
    from .vamsys import VamsysSnapshot

XLSX_NAME = "重算清单.xlsx"

SHEETS: tuple[str, ...] = ("0 总览", "1 新增", "2 调整", "3 停用", "4 计划外临时航班", "5 待观察", "6 一致",
                           "7 去回程缺失段", "8 机队与机场审计", "9 人工复核")

C = DecisionCategory
SHEET_OF_CATEGORY: dict[DecisionCategory, str] = {
    C.CREATE_PLAN: "1 新增", C.CREATE_EXTRA_SLOT: "1 新增", C.CREATE_FR24: "1 新增",
    C.UPDATE: "2 调整",
    C.RETIRE: "3 停用", C.RETIRE_REDUNDANT: "3 停用", C.RETIRE_DEFERRED: "3 停用",
    C.CHARTER: "4 计划外临时航班", C.POSITIONING: "4 计划外临时航班",
    C.WATCH_PLAN: "5 待观察", C.HOLD: "5 待观察",
    C.KEEP: "6 一致", C.KEEP_UNPLANNED: "6 一致",
    C.AIRPORT_ADD: "8 机队与机场审计",
}
ROTATION_CODES = (ReasonCode.ROTATION_GAP_LEG, ReasonCode.HOLD_ROTATION_GAP_BLIND)

_HEAD_FONT = Font(bold=True, color="FFFFFF")
_HEAD_FILL = PatternFill("solid", fgColor="305496")
_TITLE_FONT = Font(bold=True, size=12)
_CHANGED_FILL = PatternFill("solid", fgColor="FFF2CC")
_REVIEW_FILL = PatternFill("solid", fgColor="FCE4D6")
_WRAP = Alignment(wrap_text=True, vertical="top")
_TOP = Alignment(vertical="top")


def sheet_for(d: Decision) -> str:
    s = SHEET_OF_CATEGORY.get(d.category)
    if s == "6 一致" and d.changes:
        return "2 调整"
    if s is None:  # category added later without a sheet: route by its CSV file, else report-only watch list
        stem = CSV_FILE.get(d.category)
        s = {"01_airports_add": "8 机队与机场审计", "02_routes_retire": "3 停用", "02b_routes_retire_deferred": "3 停用",
             "03_routes_update": "2 调整", "04_routes_create": "1 新增"}.get(stem or "", "5 待观察")
    return s


# --------------------------------------------------------------------------------------------- cell helpers -------
class _Ctx:
    def __init__(self, meta: RunMeta, cfg: "Config", vamsys: "VamsysSnapshot | None", csv_plan: XC.CsvPlan):
        self.meta, self.cfg, self.vamsys = meta, cfg, vamsys
        self.csv_plan = csv_plan

    def fleets(self, fids) -> str:
        fids = list(fids or ())
        if not fids:
            return ""
        if self.vamsys is not None:
            return self.vamsys.fleet_labels(fids)
        return ",".join(fids)

    def csv_target(self, d: Decision) -> str:
        stem = XC.stem_for(d)
        if stem is None:
            return "不导出"
        why = XC.skip_reason(stem, d)
        if why:
            return {"already_retiring": "不导出（已设End Date）", "no_changes": "不导出（无变化）",
                    "no_csv_fields": "不导出（需手工处理）", "no_vroute_id": "不导出（缺ID）"}.get(why, f"不导出（{why}）")
        return XC.file_key(stem, d)


def _reasons(d: Decision) -> str:
    return "\n".join(RS.reason_text(r) for r in d.reasons)


def _banks(d: Decision) -> str:
    out = []
    for b in d.evidence.banks:
        out.append(f"{b.get('dep', '')}-{b.get('arr', '')} {b.get('fd3', '')} 实飞{b.get('acts', 0)}/观测{b.get('obs', 0)}"
                   f" 近期{b.get('recent_obs', 0)}")
    return "\n".join(out)


def _ac(d: Decision) -> str:
    return ",".join(f"{k}×{v}" for k, v in sorted(d.evidence.ac.items(), key=lambda kv: -kv[1]))


def _review_text(d: Decision) -> str:
    flags = set(d.review_flags)
    hits = [RS.reason_text(r) for r in d.reasons if str(r.code) in flags]
    return "\n".join(hits) if hits else ",".join(d.review_flags)


# Column spec: (header, width, getter(d, ctx), wrap)
Col = tuple[str, float, Callable[[Decision, _Ctx], Any], bool]

BASE_COLS: list[Col] = [
    ("类别", 16, lambda d, c: CATEGORY_ZH.get(d.category, str(d.category)), False),
    ("航司", 6, lambda d, c: d.target.op, False),
    ("呼号", 10, lambda d, c: d.target.callsign, False),
    ("班号", 9, lambda d, c: d.target.number, False),
    ("起", 6, lambda d, c: d.target.orig, False),
    ("降", 6, lambda d, c: d.target.dest, False),
    ("类型", 9, lambda d, c: d.target.type, False),
    ("起飞UTC", 8, lambda d, c: d.target.dep, False),
    ("到达UTC", 8, lambda d, c: d.target.arr, False),
    ("星期", 9, lambda d, c: days_to_fd3(d.target.days) if d.target.days else "", False),
    ("星期(VAMSYS)", 22, lambda d, c: days_to_vdays(d.target.days), True),
    ("机队", 28, lambda d, c: c.fleets(d.target.fleet_ids), True),
]
VID_COL: Col = ("VAMSYS ID", 11, lambda d, c: d.vroute_id or "", False)
EVIDENCE_COLS: list[Col] = [
    ("置信度", 7, lambda d, c: str(d.confidence), False),
    ("复核", 6, lambda d, c: "是" if d.needs_review else "", False),
    ("原因", 60, lambda d, c: _reasons(d), True),
    ("证据状态", 16, lambda d, c: d.evidence.state, False),
    ("实飞", 6, lambda d, c: d.evidence.acts, False),
    ("近期实飞", 8, lambda d, c: d.evidence.recent_acts, False),
    ("末次实飞", 11, lambda d, c: d.evidence.last_act, False),
    ("FR24时段", 40, lambda d, c: _banks(d), True),
    ("FR24机型", 16, lambda d, c: _ac(d), True),
]
TAIL_COLS: list[Col] = [
    ("计划来源", 34, lambda d, c: "\n".join(d.plan_refs), True),
    ("依赖机场", 10, lambda d, c: ",".join(d.depends_on_airports), False),
    ("CSV", 26, lambda d, c: c.csv_target(d), False),
]


def _write_table(ws: Worksheet, cols: list[Col], decisions: Sequence[Decision], ctx: _Ctx, *,
                 start_row: int = 1, highlight: Callable[[Decision, str], bool] | None = None,
                 freeze: bool = True, autofilter: bool = True) -> int:
    """Writes header + rows; returns the next free row."""
    for j, (name, width, _, _) in enumerate(cols, 1):
        cell = ws.cell(row=start_row, column=j, value=name)
        cell.font, cell.fill = _HEAD_FONT, _HEAD_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = width
    r = start_row
    for d in decisions:
        r += 1
        for j, (name, _, get, wrap) in enumerate(cols, 1):
            v = get(d, ctx)
            if isinstance(v, str) and v.startswith("="):
                v = "'" + v
            cell = ws.cell(row=r, column=j, value=v if v != "" else None)
            cell.alignment = _WRAP if wrap else _TOP
            if highlight is not None and highlight(d, name):
                cell.fill = _CHANGED_FILL
    if freeze:
        ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if autofilter and cols:
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(cols))}{max(r, start_row)}"
    return r + 1


def _write_kv_table(ws: Worksheet, headers: list[tuple[str, float]], rows: list[list[Any]], start_row: int,
                    title: str | None = None) -> int:
    if title:
        ws.cell(row=start_row, column=1, value=title).font = _TITLE_FONT
        start_row += 1
    for j, (h, w) in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=j, value=h)
        cell.font, cell.fill = _HEAD_FONT, _HEAD_FILL
        cur = ws.column_dimensions[get_column_letter(j)].width or 0
        ws.column_dimensions[get_column_letter(j)].width = max(cur, w)
    r = start_row
    for row in rows:
        r += 1
        for j, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=j, value=v if v != "" else None)
            cell.alignment = _WRAP if isinstance(v, str) and ("\n" in v or len(v) > 40) else _TOP
    return r + 2


# --------------------------------------------------------------------------------------------- sheets -------------
def _sheet_overview(ws: Worksheet, meta: RunMeta, decisions: Sequence[Decision], ctx: _Ctx) -> None:
    ws.cell(row=1, column=1, value=f"vCCA 航季重算清单  {meta.season_label} [{meta.season}]").font = Font(bold=True,
                                                                                                    size=14)
    closure = RS.closure_check(meta, decisions, ctx.vamsys)
    src = [
        ["航季", f"{meta.season_label}（{meta.season_start} ~ {meta.season_end}）"],
        ["计算时间", RS.fmt_utc_str(meta.generated_at)],
        ["生效时间（End/Start Date）", RS.fmt_utc_str(meta.effective_time)],
        ["CAAC 计划", f"目标航司行 {meta.plan_rows}，航段 {meta.plan_legs}，班号 {meta.plan_numbers}"],
        ["VAMSYS", f"revision {meta.vamsys_revision}（StorageRevision {meta.vamsys_storage_revision}），"
                   f"航线 {meta.vamsys_routes}，商业 {meta.vamsys_commercial_routes}"],
        ["FR24 缓存", f"{meta.fr24_rows} 行，最新航班 {RS.fmt_ts(meta.fr24_latest_ts)}，"
                      f"最后抓取 {RS.fmt_utc_str(meta.fr24_last_fetch)}"],
    ]
    if ctx.vamsys is not None and closure.get("vamsys_changed_since_build"):
        src.append(["!! VAMSYS 已变化", f"现在 revision {ctx.vamsys.revision}，与 build 时不同，建议重新 build"])
    r = _write_kv_table(ws, [("项目", 26), ("内容", 90)], src, 3, "数据源")

    counts = Counter(str(d.category) for d in decisions)
    csv_counts = ctx.csv_plan.counts
    rows = []
    for cat in RS.ordered_categories(counts):
        n = counts.get(cat, 0)
        stem = CSV_FILE.get(DecisionCategory(cat)) if cat in {str(x) for x in DecisionCategory} else None
        rv = sum(1 for d in decisions if str(d.category) == cat and d.needs_review)
        rows.append([cat, RS.category_zh(cat), n, rv, stem or "只列报告"])
    rows.append(["合计", "", sum(counts.values()), sum(1 for d in decisions if d.needs_review), ""])
    r = _write_kv_table(ws, [("类别", 26), ("说明", 90), ("数量", 10), ("待复核", 10), ("CSV", 28)], rows, r,
                        "决策分类")

    cl = [["结果", "通过" if closure["ok"] else "不平！"],
          ["已决 VAMSYS 商业航线", closure["decided"]], ["应有（build 时）", closure["expected"]],
          ["重复去向", ",".join(closure["duplicates"]) or "无"], ["缺 VAMSYS ID", closure["missing_vroute_id"]]]
    if "undecided_now" in closure:
        cl.append(["现 VAMSYS 中未决", ",".join(closure["undecided_now"][:50]) or "无"])
        cl.append(["已决但现 VAMSYS 中不存在", ",".join(closure["not_in_vamsys_now"][:50]) or "无"])
    r = _write_kv_table(ws, [("去向核账", 26), ("", 90)], cl, r, "去向核账（每条商业航线恰好一个去向）")

    csv_rows = [[XC.csv_filename(k, n), n] for k, n in csv_counts.items()] or [["（无需导入的变更）", 0]]
    csv_rows += [[f"跳过：{k}", v] for k, v in ctx.csv_plan.skipped.items()]
    r = _write_kv_table(ws, [("CSV 文件", 26), ("行数", 90)], csv_rows, r, "FlightOpsDesk CSV")

    th = []
    names = list(meta.thresholds) + [k for k in meta.threshold_hits if k not in meta.thresholds]
    for k in names:
        v = meta.thresholds.get(k, "")
        th.append([k, str(v) if v != "" else "", meta.threshold_hits.get(k, "")])
    r = _write_kv_table(ws, [("阈值", 26), ("取值", 90), ("命中航线", 10)], th, r, "阈值与命中统计")

    warns = list(meta.warnings) + ctx.csv_plan.warnings + RS.decision_warnings(meta, decisions)
    if warns:
        _write_kv_table(ws, [("警告", 26)], [[w] for w in warns], r, "警告")
    ws.freeze_panes = "A2"


def _changed(field: str) -> Callable[[Decision, _Ctx], Any]:
    def get(d: Decision, c: _Ctx, *, side: str):
        for ch in d.changes:
            if ch.field == field:
                v = ch.old if side == "old" else ch.new
                return c.fleets(v or ()) if field == "fleet_ids" else RS.fmt_value(field, v)
        return ""
    return get


def _update_cols() -> list[Col]:
    cols: list[Col] = [VID_COL] + BASE_COLS[:7]
    cols.append(("变更字段", 14, lambda d, c: ",".join(ch.field for ch in d.changes), True))
    for f, zh, w in (("dep", "起飞", 8), ("arr", "到达", 8), ("days", "星期", 9), ("fleet_ids", "机队", 26)):
        g = _changed(f)
        cols.append((f"现行{zh}", w, lambda d, c, g=g: g(d, c, side="old"), f == "fleet_ids"))
        cols.append((f"推荐{zh}", w, lambda d, c, g=g: g(d, c, side="new"), f == "fleet_ids"))
    cols.append(("其他变更", 26, lambda d, c: "\n".join(
        f"{ch.field}：{RS.fmt_value(ch.field, ch.old)} → {RS.fmt_value(ch.field, ch.new)}"
        for ch in d.changes if ch.field not in ("dep", "arr", "days", "fleet_ids")), True))
    cols.append(("推荐星期(VAMSYS)", 22, lambda d, c: days_to_vdays(d.target.days), True))
    cols.append(("Cost Index", 8, lambda d, c: d.current.cost_index if d.current else d.target.cost_index, False))
    return cols + EVIDENCE_COLS + TAIL_COLS


def _update_highlight(d: Decision, col: str) -> bool:
    fields = {ch.field for ch in d.changes}
    return any(col.endswith(zh) and f in fields and col.startswith("推荐")
               for f, zh in (("dep", "起飞"), ("arr", "到达"), ("days", "星期"), ("fleet_ids", "机队")))


def _retire_cols() -> list[Col]:
    return [VID_COL] + BASE_COLS + [
        ("现行End Date", 19, lambda d, c: d.current.end_date if d.current else "", False),
        ("停用生效", 19, lambda d, c: d.target.end_date, False),
    ] + EVIDENCE_COLS + TAIL_COLS


def _create_cols() -> list[Col]:
    def plan_time(d, c):
        det = d.details or {}
        if det.get("plan_dep"):
            days = det.get("plan_days") or []
            return f"{det.get('plan_dep')}-{det.get('plan_arr', '')} {days_to_fd3(days) if days else ''}".strip()
        return ""
    return BASE_COLS + [
        ("时段", 6, lambda d, c: d.slot_index, False),
        ("计划时刻(UTC)", 20, plan_time, False),
        ("Cost Index", 8, lambda d, c: d.target.cost_index, False),
        ("Start Date", 19, lambda d, c: d.target.start_date, False),
    ] + EVIDENCE_COLS + TAIL_COLS


def _charter_cols() -> list[Col]:
    return BASE_COLS[:10] + [
        ("日期", 30, lambda d, c: ",".join(d.evidence.dates), True),
        ("首次", 11, lambda d, c: (d.details or {}).get("first") or (d.evidence.dates[0] if d.evidence.dates else ""),
         False),
        ("末次", 11, lambda d, c: (d.details or {}).get("last") or (d.evidence.dates[-1] if d.evidence.dates else ""),
         False),
        ("周数", 6, lambda d, c: (d.details or {}).get("weeks", ""), False),
        ("机型", 16, lambda d, c: _ac(d), True),
        ("注册号", 24, lambda d, c: ",".join(d.evidence.regs), True),
        ("他司同班号", 20, lambda d, c: ",".join("/".join(map(str, x)) for x in d.evidence.other_ops), True),
    ] + EVIDENCE_COLS[:6]


def _simple_cols() -> list[Col]:
    return [VID_COL] + BASE_COLS + EVIDENCE_COLS + TAIL_COLS


def _rotation_cols() -> list[Col]:
    return [VID_COL] + BASE_COLS[:10] + [
        ("缺失段说明", 40, lambda d, c: "\n".join(RS.reason_text(r) for r in d.reasons
                                               if str(r.code) in {str(x) for x in ROTATION_CODES}), True),
        ("处理", 20, lambda d, c: c.csv_target(d), False),
    ] + EVIDENCE_COLS + TAIL_COLS


def _review_cols() -> list[Col]:
    return [VID_COL] + BASE_COLS[:10] + [
        ("复核标记", 20, lambda d, c: "\n".join(d.review_flags), True),
        ("复核原因", 50, lambda d, c: _review_text(d), True),
        ("变更", 40, lambda d, c: "\n".join(
            f"{ch.field}：{RS.fmt_value(ch.field, ch.old)} → {RS.fmt_value(ch.field, ch.new)}" for ch in d.changes),
         True),
    ] + EVIDENCE_COLS + TAIL_COLS


def _sheet_audit(ws: Worksheet, meta: RunMeta, decisions: Sequence[Decision], ctx: _Ctx) -> None:
    r = 1
    if ctx.vamsys is not None:
        from .fleets import fleet_usage

        usage = fleet_usage(ctx.cfg, ctx.vamsys, decisions)
        rows = [[u["fleet_id"], u["type_code"], u["kind"], u["aircraft"], u["routes_now"], u["routes_after"],
                 "是" if u["in_rules"] else "否", u["note"]] for u in usage]
        r = _write_kv_table(ws, [("机队ID", 10), ("机型", 10), ("类别", 8), ("飞机数", 8), ("现航线数", 10),
                                 ("执行后航线数", 12), ("规则内", 8), ("备注", 30)], rows, r,
                            f"VAMSYS 机队使用情况（{len(rows)} 个）")
    else:
        ws.cell(row=r, column=1, value="（未提供 VAMSYS 快照，跳过机队使用统计）")
        r += 2

    rules = ctx.cfg.airport_rules
    retire = {C.RETIRE, C.RETIRE_REDUNDANT, C.RETIRE_DEFERRED}
    unrated: dict[str, list[str]] = {}
    for d in decisions:
        if d.category in retire or d.category == C.AIRPORT_ADD or not d.target.dest:
            continue
        for a in (d.target.orig, d.target.dest):
            if a and not rules.is_rated(a):
                unrated.setdefault(a, []).append(RS.route_label(d.target))
    rows = []
    for a, routes in sorted(unrated.items()):
        name = ctx.vamsys.airports[a].name if ctx.vamsys is not None and a in ctx.vamsys.airports else ""
        rows.append([a, name, len(routes), "\n".join(sorted(set(routes))[:12]) + (" ..." if len(set(routes)) > 12 else "")])
    r = _write_kv_table(ws, [("机场", 10), ("名称", 10), ("航线数", 8), ("涉及航线", 8)],
                        rows or [["（无）", "", "", ""]], r, f"未定级机场（{len(rows)} 个，机队暂按 4C 窄体池）")

    adds = [d for d in decisions if d.category == C.AIRPORT_ADD]
    deps: dict[str, list[str]] = {}
    for d in decisions:
        for a in d.depends_on_airports:
            deps.setdefault(a, []).append(RS.route_label(d.target))
    rows = [[d.target.orig, (d.details or {}).get("name", ""), len(deps.get(d.target.orig, [])),
             "\n".join(sorted(set(deps.get(d.target.orig, [])))[:12]), ctx.csv_target(d),
             "是" if d.needs_review else ""] for d in adds]
    if ctx.vamsys is not None:  # targets pointing at airports missing from VAMSYS and not in AIRPORT_ADD
        added = {d.target.orig for d in adds}
        miss: dict[str, list[str]] = {}
        for d in decisions:
            if d.category in retire or d.category == C.AIRPORT_ADD or not d.target.dest:
                continue
            for a in (d.target.orig, d.target.dest):
                if a and a not in added and not ctx.vamsys.has_airport(a):
                    miss.setdefault(a, []).append(f"{RS.route_label(d.target)}（{RS.category_zh(d.category)}）")
        rows += [[a, "（未列入新增）", len(v), "\n".join(sorted(set(v))[:12]), "", ""] for a, v in sorted(miss.items())]
    _write_kv_table(ws, [("机场", 10), ("名称", 10), ("航线数", 8), ("涉及航线", 8), ("CSV", 26), ("复核", 6)],
                    rows or [["（无）", "", "", "", "", ""]], r, "VAMSYS 缺场（01 新增机场）")
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["D"].width = 40
    ws.column_dimensions["H"].width = 30


# --------------------------------------------------------------------------------------------- entry --------------
def build_workbook(meta: RunMeta, decisions: Sequence[Decision], cfg: "Config",
                   vamsys: "VamsysSnapshot | None" = None) -> Workbook:
    ctx = _Ctx(meta, cfg, vamsys, XC.plan_csv(meta, decisions, cfg))
    wb = Workbook()
    wb.remove(wb.active)
    ws = {name: wb.create_sheet(name) for name in SHEETS}
    by_sheet: dict[str, list[Decision]] = {name: [] for name in SHEETS}
    for d in decisions:
        by_sheet[sheet_for(d)].append(d)

    def sort_key(d: Decision):
        return (str(d.category), d.target.op, d.target.callsign, d.target.orig, d.target.dest, d.target.dep)

    _sheet_overview(ws["0 总览"], meta, decisions, ctx)
    _write_table(ws["1 新增"], _create_cols(), sorted(by_sheet["1 新增"], key=sort_key), ctx)
    _write_table(ws["2 调整"], _update_cols(), sorted(by_sheet["2 调整"], key=sort_key), ctx,
                 highlight=_update_highlight)
    _write_table(ws["3 停用"], _retire_cols(), sorted(by_sheet["3 停用"], key=sort_key), ctx)
    _write_table(ws["4 计划外临时航班"], _charter_cols(), sorted(by_sheet["4 计划外临时航班"], key=sort_key), ctx)
    _write_table(ws["5 待观察"], _simple_cols(), sorted(by_sheet["5 待观察"], key=sort_key), ctx)
    _write_table(ws["6 一致"], _simple_cols(), sorted(by_sheet["6 一致"], key=sort_key), ctx)
    rot = [d for d in decisions if RS.has_reason(d, *ROTATION_CODES)]
    _write_table(ws["7 去回程缺失段"], _rotation_cols(), sorted(rot, key=sort_key), ctx)
    _sheet_audit(ws["8 机队与机场审计"], meta, decisions, ctx)
    review = [d for d in decisions if d.needs_review]
    _write_table(ws["9 人工复核"], _review_cols(),
                 sorted(review, key=lambda d: (",".join(d.review_flags), *sort_key(d))), ctx)
    ws["9 人工复核"].sheet_properties.tabColor = "ED7D31"
    return wb


def export_xlsx(run_dir: Path, meta: RunMeta, decisions: Sequence[Decision], cfg: "Config",
                vamsys: "VamsysSnapshot | None" = None) -> Path:
    """Write <run_dir>/重算清单.xlsx and return its path. ``vamsys`` (optional) enriches sheet 8."""
    wb = build_workbook(meta, decisions, cfg, vamsys)
    p = Path(run_dir) / XLSX_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.stem + ".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(p)
    return p
