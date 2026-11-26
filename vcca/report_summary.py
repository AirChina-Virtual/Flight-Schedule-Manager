"""summary.json + run.log helpers (plan 7.4; step 6), plus small display helpers shared by the exporters.

summary.json schema:
  {"season", "season_label", "generated_at", "exported_at", "effective_time", "vamsys_revision",
   "plan": {"rows","legs","numbers"}, "fr24": {"rows","latest","last_fetch"},
   "counts": {category: n}, "csv": {file key: n}, "files": [names], "needs_review": n,
   "review_flags": {code: n}, "closure_ok": bool, "closure": {...},
   "threshold_hits": {...}, "warnings": [...]}
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .models import (CATEGORY_ZH, VROUTE_CATEGORIES, Decision, DecisionCategory, EvidenceState, Reason, ReasonCode,
                     RouteTarget, RunMeta, describe_reason)
from .util_time import days_to_fd3

if TYPE_CHECKING:
    from .config import Config
    from .vamsys import VamsysSnapshot

SUMMARY_NAME = "summary.json"
LOG_NAME = "run.log"

BJT = _dt.timezone(_dt.timedelta(hours=8))


# =====================================================================================================================
# Display helpers (output layer only)
# =====================================================================================================================
def category_zh(cat: str) -> str:
    try:
        return CATEGORY_ZH.get(DecisionCategory(cat), str(cat))
    except ValueError:
        return str(cat)


def ordered_categories(extra: Iterable[str] = ()) -> list[str]:
    """Every DecisionCategory member in enum order, then any unknown names seen in the data."""
    out = [str(c) for c in DecisionCategory]
    for c in extra:
        if str(c) not in out:
            out.append(str(c))
    return out


def fmt_days(days: Iterable[int]) -> str:
    return days_to_fd3(days) if days is not None else ""


def fmt_value(field: str, v: Any) -> str:
    """FieldChange value -> display text."""
    if v is None:
        return ""
    if field == "days":
        return days_to_fd3(v) if v else "（空）"
    if field == "fleet_ids":
        return ",".join(str(x) for x in v) if v else "（空）"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if v == "":
        return "（空）"
    return str(v)


def route_label(t: RouteTarget) -> str:
    if not t.callsign and not t.dest:
        return t.orig
    return f"{t.callsign} {t.orig}-{t.dest}"


def fmt_ts(ts: int | None) -> str:
    """UNIX seconds -> 'YYYY-MM-DD HH:MM BJT (HH:MMZ)'; None -> ''."""
    if ts is None or ts == "":
        return ""
    try:
        u = _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts)
    return f"{u.astimezone(BJT):%Y-%m-%d %H:%M} BJT ({u:%m-%d %H:%M}Z)"


def fmt_utc_str(s: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> same + BJT."""
    if not s:
        return ""
    try:
        u = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return s
    return f"{s} UTC（北京时间 {u.astimezone(BJT):%Y-%m-%d %H:%M}）"


_DAY_PARAMS = frozenset({"old", "new", "days", "plan_days"})


def _is_day_list(v: Any) -> bool:
    return isinstance(v, (list, tuple)) and all(isinstance(x, int) and 0 <= x <= 6 for x in v)


def reason_text(r: Reason) -> str:
    """describe_reason for display, with weekday lists (which come back from JSON as int lists) shown as days.
    FIELD_CHANGED is rendered with fmt_value so days / fleets / empty values read naturally."""
    p = r.params or {}
    if str(r.code) == str(ReasonCode.FIELD_CHANGED):
        f = str(p.get("field", "?"))
        return f"{f}：{fmt_value(f, p.get('old'))} → {fmt_value(f, p.get('new'))}"
    if any(k in _DAY_PARAMS and _is_day_list(v) for k, v in p.items()):
        p = {k: (frozenset(v) if k in _DAY_PARAMS and _is_day_list(v) else v) for k, v in p.items()}
        r = Reason(r.code, p)
    return describe_reason(r)


def has_reason(d: Decision, *codes: str) -> bool:
    cs = {str(c) for c in codes}
    return any(str(r.code) in cs for r in d.reasons)


# =====================================================================================================================
# Checks
# =====================================================================================================================
def closure_check(meta: RunMeta, decisions: Sequence[Decision], vamsys: "VamsysSnapshot | None" = None
                  ) -> dict[str, Any]:
    """Plan 5.8 E: every commercial VAMSYS route appears in exactly one VROUTE_CATEGORIES decision."""
    ids = [d.vroute_id for d in decisions if d.category in VROUTE_CATEGORIES]
    missing_id = sum(1 for x in ids if not x)
    cnt = Counter(x for x in ids if x)
    dup = sorted(k for k, v in cnt.items() if v > 1)
    stray = sorted(d.vroute_id for d in decisions if d.vroute_id and d.category not in VROUTE_CATEGORIES)
    res: dict[str, Any] = {"decided": len(cnt), "expected": meta.vamsys_commercial_routes, "duplicates": dup,
                           "missing_vroute_id": missing_id, "stray_vroute_id": stray}
    ok = not dup and not missing_id and len(cnt) == meta.vamsys_commercial_routes
    if vamsys is not None:
        com = {r.id for r in vamsys.commercial_routes}
        res["vamsys_revision_now"] = vamsys.revision
        res["vamsys_changed_since_build"] = (meta.vamsys_revision is not None
                                             and vamsys.revision != meta.vamsys_revision)
        res["not_in_vamsys_now"] = sorted(set(cnt) - com)
        res["undecided_now"] = sorted(com - set(cnt))
        if not res["vamsys_changed_since_build"]:
            ok = ok and not res["not_in_vamsys_now"] and not res["undecided_now"]
    res["ok"] = ok
    return res


def decision_warnings(meta: RunMeta, decisions: Sequence[Decision]) -> list[str]:
    out = []
    nd = sum(1 for d in decisions if d.evidence.state == str(EvidenceState.NO_DATA))
    if nd:
        out.append(f"{nd} 条决策的 FR24 证据为 no_data（未抓到数据），建议补抓后重算")
    return out


# =====================================================================================================================
# Summary
# =====================================================================================================================
def summarize(meta: RunMeta, decisions: Sequence[Decision], *, csv_counts: dict[str, int] | None = None,
              files: Iterable[Path | str] = (), warnings: Iterable[str] = (),
              vamsys: "VamsysSnapshot | None" = None) -> dict[str, Any]:
    """Build the summary dict (schema in module doc)."""
    counts = Counter(str(d.category) for d in decisions)
    review = [d for d in decisions if d.needs_review]
    flags = Counter(f for d in review for f in d.review_flags)
    closure = closure_check(meta, decisions, vamsys)
    warns = list(meta.warnings) + decision_warnings(meta, decisions) + list(warnings)
    if not closure["ok"]:
        warns.append(f"去向核账不平：已决 {closure['decided']} / 应有 {closure['expected']}，"
                     f"重复 {len(closure['duplicates'])}，缺ID {closure['missing_vroute_id']}")
    if closure.get("vamsys_changed_since_build"):
        warns.append(f"VAMSYS 已变化（build 时 revision {meta.vamsys_revision}，现在 {closure['vamsys_revision_now']}），"
                     "建议重新 build 后再导入 CSV")
    return {
        "season": meta.season,
        "season_label": meta.season_label,
        "generated_at": meta.generated_at,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "effective_time": meta.effective_time,
        "vamsys_revision": meta.vamsys_revision,
        "plan": {"rows": meta.plan_rows, "legs": meta.plan_legs, "numbers": meta.plan_numbers},
        "fr24": {"rows": meta.fr24_rows, "latest": fmt_ts(meta.fr24_latest_ts), "last_fetch": meta.fr24_last_fetch},
        "counts": {c: counts[c] for c in ordered_categories(counts) if counts.get(c)},
        "csv": dict(csv_counts or {}),
        "files": [Path(p).name for p in files],
        "needs_review": len(review),
        "review_flags": dict(flags.most_common()),
        "closure_ok": bool(closure["ok"]),
        "closure": closure,
        "threshold_hits": dict(meta.threshold_hits),
        "warnings": warns,
    }


def write_summary(run_dir: Path, meta: RunMeta, decisions: Sequence[Decision], *,
                  csv_counts: dict[str, int] | None = None, files: Iterable[Path | str] = (),
                  warnings: Iterable[str] = (), vamsys: "VamsysSnapshot | None" = None,
                  summary: dict[str, Any] | None = None) -> Path:
    s = summary if summary is not None else summarize(meta, decisions, csv_counts=csv_counts, files=files,
                                                      warnings=warnings, vamsys=vamsys)
    p = Path(run_dir) / SUMMARY_NAME
    p.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def print_summary(summary: dict[str, Any]) -> None:
    """Console summary (Chinese labels via models.CATEGORY_ZH)."""
    s = summary
    print(f"\n==== 导出汇总  {s.get('season_label', '')} [{s.get('season', '')}] ====")
    print(f"计算时间 {s.get('generated_at')} UTC   生效时间 {s.get('effective_time')} UTC"
          f"   VAMSYS revision {s.get('vamsys_revision')}")
    print("决策分类：")
    for cat, n in s.get("counts", {}).items():
        print(f"  {category_zh(cat):<18} {cat:<18} {n:>5}")
    print(f"去向核账  {'通过' if s.get('closure_ok') else '不平！'}"
          f"（{s.get('closure', {}).get('decided')} / {s.get('closure', {}).get('expected')}）")
    print(f"人工复核  {s.get('needs_review', 0)} 条" +
          (f"（{', '.join(f'{k} {v}' for k, v in s.get('review_flags', {}).items())}）" if s.get("review_flags") else ""))
    if s.get("csv"):
        print("CSV：")
        for k, n in s["csv"].items():
            print(f"  {k:<34} {n:>5} 行")
    else:
        print("CSV：无（没有需要导入的变更，或自检失败）")
    if s.get("files"):
        print("文件：" + "  ".join(s["files"]))
    for w in s.get("warnings", []):
        print(f"!! {w}")


def setup_run_log(run_dir: Path, name: str = "vcca") -> logging.Logger:
    """Logger writing to <run_dir>/run.log (UTF-8, appended) and the console. Implemented."""
    run_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    target = str((run_dir / LOG_NAME).resolve())
    if not any(isinstance(h, logging.FileHandler) and h.baseFilename == target for h in logger.handlers):
        fh = logging.FileHandler(target, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sh)
    return logger
