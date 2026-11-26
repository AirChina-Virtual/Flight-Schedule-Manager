"""Self-contained HTML report ``report.html`` (plan 7.3; step 6).

One file, inline CSS/JS (``vcca/web/report.css`` + ``report.js``, the renderer shared with the web app, inlined by
``render_html``), no external requests (works offline): overview cards (RunMeta + counts), a table
per decision category with search / filter / sort, per-route detail (plan / VAMSYS current / recommended / FR24
evidence side by side, reasons via models.describe_reason), and a diff against the previous run
(``diff_runs``: added / removed / changed decisions keyed by vroute_id, else route key + slot).
The decisions are embedded as one JSON blob and rendered client-side (incremental rendering keeps ~3000 rows fast).
Light / dark follow ``prefers-color-scheme``; a header button overrides it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from . import export_csv as XC
from . import report_summary as RS
from .models import CATEGORY_ZH, CSV_FILE, Decision, DecisionCategory, RunMeta, load_decisions
from .util_time import days_to_fd3, ts_utc_date

if TYPE_CHECKING:
    from .config import Config
    from .vamsys import VamsysSnapshot

HTML_NAME = "report.html"

#: RouteTarget fields compared by diff_runs (start/end dates are excluded: they follow the run's effective time)
DIFF_FIELDS = ("type", "dep", "arr", "days", "fleet_ids", "cost_index", "hidden")


# =====================================================================================================================
# diff
# =====================================================================================================================
def decision_key(d: Decision) -> str:
    """Stable identity of a decision across runs: VAMSYS route ID, else route key + slot (or dep time)."""
    if d.vroute_id:
        return f"V:{d.vroute_id}"
    t = d.target
    if d.category == DecisionCategory.AIRPORT_ADD:
        return f"A:{t.orig}"
    key = d.group_key or t.key
    return "K:" + "|".join(key) + "|" + (d.slot_index or t.dep)


def _keyed(decisions: Sequence[Decision]) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    seen: Counter = Counter()
    for d in decisions:
        k = decision_key(d)
        seen[k] += 1
        out[k if seen[k] == 1 else f"{k}#{seen[k]}"] = d
    return out


def _brief(key: str, d: Decision) -> dict[str, Any]:
    t = d.target
    return {"key": key, "category": str(d.category), "category_zh": RS.category_zh(str(d.category)),
            "vroute_id": d.vroute_id, "label": RS.route_label(t), "op": t.op, "callsign": t.callsign,
            "orig": t.orig, "dest": t.dest, "dep": t.dep, "arr": t.arr,
            "days": days_to_fd3(t.days) if t.days else "", "needs_review": d.needs_review}


def _fval(field: str, v: Any) -> Any:
    if field == "days":
        return sorted(v)
    if field == "fleet_ids":
        return sorted(v)
    return v


def diff_runs(prev: Sequence[Decision], cur: Sequence[Decision]) -> dict[str, list[dict]]:
    """{'added': [...], 'removed': [...], 'changed': [...]} decision summaries between two runs.
    ``changed`` entries carry ``prev_category`` and ``diffs`` ([{'field','old','new'}] with display strings)."""
    a, b = _keyed(prev), _keyed(cur)
    added = [_brief(k, d) for k, d in b.items() if k not in a]
    removed = [_brief(k, d) for k, d in a.items() if k not in b]
    changed = []
    for k, d in b.items():
        p = a.get(k)
        if p is None:
            continue
        diffs = []
        if p.category != d.category:
            diffs.append({"field": "category", "old": RS.category_zh(str(p.category)),
                          "new": RS.category_zh(str(d.category))})
        for f in DIFF_FIELDS:
            ov, nv = _fval(f, getattr(p.target, f)), _fval(f, getattr(d.target, f))
            if ov != nv:
                diffs.append({"field": f, "old": RS.fmt_value(f, ov), "new": RS.fmt_value(f, nv)})
        pc, dc = sorted(c.field for c in p.changes), sorted(c.field for c in d.changes)
        if pc != dc:
            diffs.append({"field": "changes", "old": ",".join(pc) or "（无）", "new": ",".join(dc) or "（无）"})
        if p.needs_review != d.needs_review or sorted(p.review_flags) != sorted(d.review_flags):
            diffs.append({"field": "review", "old": ",".join(p.review_flags) if p.needs_review else "否",
                          "new": ",".join(d.review_flags) if d.needs_review else "否"})
        if p.evidence.state != d.evidence.state:
            diffs.append({"field": "evidence", "old": p.evidence.state, "new": d.evidence.state})
        if diffs:
            e = _brief(k, d)
            e["prev_category"] = str(p.category)
            e["diffs"] = diffs
            changed.append(e)
    order = {c: i for i, c in enumerate(RS.ordered_categories())}
    for lst in (added, removed, changed):
        lst.sort(key=lambda e: (order.get(e["category"], 99), e["op"], e["callsign"], e["orig"], e["dest"], e["dep"]))
    return {"added": added, "removed": removed, "changed": changed}


# =====================================================================================================================
# payload
# =====================================================================================================================
def _tgt(t, fleet_label) -> dict[str, Any]:
    return {"op": t.op, "cs": t.callsign, "num": t.number, "o": t.orig, "d": t.dest, "ty": t.type, "dep": t.dep,
            "arr": t.arr, "days": days_to_fd3(t.days) if t.days else "", "fl": fleet_label(t.fleet_ids),
            "ci": "" if t.cost_index is None else t.cost_index, "hid": t.hidden, "sd": t.start_date,
            "ed": t.end_date}


def _row(i: int, d: Decision, fleet_label, csv_target: str) -> dict[str, Any]:
    t = d.target
    det = d.details or {}
    ev = d.evidence
    plan = None
    if d.group_key or d.plan_refs:
        plan = {"key": " ".join(d.group_key) if d.group_key else "", "refs": d.plan_refs,
                "dep": det.get("plan_dep", ""), "arr": det.get("plan_arr", ""),
                "days": days_to_fd3(det["plan_days"]) if det.get("plan_days") else ""}
    r: dict[str, Any] = {
        "i": i, "c": str(d.category), "v": d.vroute_id or "", "op": t.op, "cs": t.callsign, "num": t.number,
        "o": t.orig, "d": t.dest, "ty": t.type, "dep": t.dep, "arr": t.arr,
        "days": days_to_fd3(t.days) if t.days else "", "conf": str(d.confidence), "rv": int(d.needs_review),
        "rf": d.review_flags, "rz": [RS.reason_text(x) for x in d.reasons], "rc": [str(x.code) for x in d.reasons],
        "st": ev.state, "acts": ev.acts, "ra": ev.recent_acts, "la": ev.last_act, "csv": csv_target,
        "ch": [{"f": c.field, "o": RS.fmt_value(c.field, c.old), "n": RS.fmt_value(c.field, c.new)}
               for c in d.changes],
        "tgt": _tgt(t, fleet_label),
        "cur": _tgt(d.current, fleet_label) if d.current is not None else None,
        "ev": {"obs": ev.obs, "banks": ev.banks, "ac": ev.ac, "regs": ev.regs, "dates": ev.dates,
               "oo": ev.other_ops, "od": ev.other_ods, "cal": ev.cal},
        "plan": plan, "dap": d.depends_on_airports, "si": d.slot_index,
        "det": {k: v for k, v in det.items() if k not in ("plan_dep", "plan_arr", "plan_days")},
    }
    if r["cur"] == r["tgt"]:
        r["cur"] = "same"
    return r


def build_payload(meta: RunMeta, decisions: Sequence[Decision], cfg: "Config | None" = None, *,
                  vamsys: "VamsysSnapshot | None" = None, previous: tuple[str, Sequence[Decision]] | None = None
                  ) -> dict[str, Any]:
    csv_plan = XC.plan_csv(meta, decisions, cfg)
    if vamsys is not None:
        def fleet_label(fids):
            return vamsys.fleet_labels(fids) if fids else ""
    else:
        def fleet_label(fids):
            return ",".join(fids)

    def csv_target(d: Decision) -> str:
        stem = XC.stem_for(d)
        if stem is None:
            return ""
        why = XC.skip_reason(stem, d)
        return f"不导出（{why}）" if why else XC.file_key(stem, d)

    counts = Counter(str(d.category) for d in decisions)
    cats = []
    for c in RS.ordered_categories(counts):
        try:
            stem = CSV_FILE.get(DecisionCategory(c))
        except ValueError:
            stem = None
        cats.append({"code": c, "zh": RS.category_zh(c), "csv": stem or "", "n": counts.get(c, 0),
                     "rv": sum(1 for d in decisions if str(d.category) == c and d.needs_review)})
    summary = RS.summarize(meta, decisions, csv_counts=csv_plan.counts, warnings=csv_plan.warnings, vamsys=vamsys)
    payload: dict[str, Any] = {
        "meta": meta.to_dict(),
        "meta_fmt": {"generated_at": RS.fmt_utc_str(meta.generated_at),
                     "effective_time": RS.fmt_utc_str(meta.effective_time),
                     "fr24_latest": RS.fmt_ts(meta.fr24_latest_ts),
                     "fr24_last_fetch": RS.fmt_utc_str(meta.fr24_last_fetch)},
        "summary": summary,
        "csv": [{"key": k, "name": XC.csv_filename(k, n), "n": n} for k, n in csv_plan.counts.items()],
        "cats": cats,
        "rows": [_row(i, d, fleet_label, csv_target(d)) for i, d in enumerate(decisions)],
        # calendar frame of the per-day FR24 grid: season dates + last day with FR24 data (UTC)
        "cal_range": {"start": meta.season_start, "end": meta.season_end,
                      "latest": ts_utc_date(meta.fr24_latest_ts) if meta.fr24_latest_ts else "",
                      "tol": cfg.thresholds.plan_attach[0] if cfg is not None else 45},
        "diff": None,
    }
    if previous is not None:
        name, prev = previous
        dr = diff_runs(prev, decisions)
        payload["diff"] = {"prev_run": name, **dr}
    return payload


def previous_run(cfg: "Config", run_dir: Path) -> Path | None:
    """The newest run older than ``run_dir`` that has a decisions.json (what export diffs against)."""
    prev = [r for r in cfg.list_runs() if (r / "decisions.json").is_file() and r.name < Path(run_dir).name]
    return prev[-1] if prev else None


def payload_for_run(cfg: "Config", run_dir: Path, *, prev: str | Path | None = "auto",
                    vamsys: "VamsysSnapshot | None" = None) -> dict[str, Any]:
    """Report payload of one run (shared by ``export`` and the web app).

    prev: "auto" = the previous run with decisions.json (export behaviour), a run name / path = diff against that run,
    None = no diff. ``vamsys`` (optional) labels fleets like the exported report."""
    run_dir = Path(run_dir)
    meta, decisions = load_decisions(run_dir / "decisions.json")
    if prev == "auto":
        prev_dir = previous_run(cfg, run_dir)
    elif prev:
        prev_dir = cfg.resolve_run(str(prev))
    else:
        prev_dir = None
    previous = None
    if prev_dir is not None and (prev_dir / "decisions.json").is_file():
        _, pd = load_decisions(prev_dir / "decisions.json")
        previous = (prev_dir.name, pd)
    return build_payload(meta, decisions, cfg, vamsys=vamsys, previous=previous)


def _json_for_script(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    # keep the blob inert inside <script type="application/json">
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_html(payload: dict[str, Any]) -> str:
    """Single offline HTML file: shell + inlined report.css / report.js + the payload JSON."""
    title = f"vCCA 重算报告 {payload['meta'].get('season', '')} {payload['meta'].get('generated_at', '')[:16]}"
    css, js = report_assets()
    if "</script" in js.lower() or "</style" in css.lower():
        raise ValueError("report.js / report.css must not contain closing script/style tags")
    parts = {"__TITLE__": _esc(title), "__CSS__": css, "__JS__": js, "__DATA__": _json_for_script(payload)}
    out, rest = [], _TEMPLATE
    for key in ("__TITLE__", "__CSS__", "__DATA__", "__JS__"):   # template order; values are never re-scanned
        head, rest = rest.split(key, 1)
        out += [head, parts[key]]
    out.append(rest)
    return "".join(out)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def export_html(run_dir: Path, meta: RunMeta, decisions: Sequence[Decision], cfg: "Config",
                *, previous_run_dir: Path | None = None, vamsys: "VamsysSnapshot | None" = None) -> Path:
    """Write <run_dir>/report.html and return its path."""
    previous = None
    if previous_run_dir is not None and (Path(previous_run_dir) / "decisions.json").is_file():
        _, prev = load_decisions(Path(previous_run_dir) / "decisions.json")
        previous = (Path(previous_run_dir).name, prev)
    payload = build_payload(meta, decisions, cfg, vamsys=vamsys, previous=previous)
    p = Path(run_dir) / HTML_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(render_html(payload), encoding="utf-8")
    tmp.replace(p)
    return p


# =====================================================================================================================
# template (no external resources): the shell below + vcca/web/report.css + report.js, inlined at render time
# =====================================================================================================================
WEB_DIR = Path(__file__).resolve().parent / "web"

_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
<div id="vcca-report"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
__JS__
VccaReport.mount(document.getElementById("vcca-report"), JSON.parse(document.getElementById("data").textContent),
  {header: true, themeButton: true, hash: true});
</script>
</body>
</html>
"""


def report_assets() -> tuple[str, str]:
    """(report.css, report.js) text — the renderer shared with the web app."""
    return ((WEB_DIR / "report.css").read_text(encoding="utf-8"),
            (WEB_DIR / "report.js").read_text(encoding="utf-8"))
