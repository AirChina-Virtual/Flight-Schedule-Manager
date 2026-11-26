"""FR24 fetch orchestration: targets, refresh tiers, rate-limited queue, resume (plan 6.1-6.3; step 3).

Targets (plan 6.1):
  flight numbers = CAAC plan numbers  ∪  VAMSYS commercial target-op numbers  ∪  numbers discovered in the cache
                   (rows since season start whose callsign belongs to a target op; deduplicated by number);
  registrations  = regs that flew a target-op flight (actual) since season start (from the store), auto-added;
                   dropped after ``cfg.fetch.reg_drop_after_days`` unseen. CAO (cargo-op) regs every run; all regs
                   with --deep or every ``cfg.fetch.deep_scan_every_days`` (meta ``last_deep_scan_at``); regs never
                   fetched (or whose last fetch failed) are always due.
  boards/search  = only when requested (--boards ICAO,... / --search). Search queries = 5-char prefixes of the
                   flight targets (as the former fetch_search_index.py script), stored in the store's search_queries table.

Tiers (plan 6.2, priorities stored in the ``targets`` table; P1/P2 written back by reconcile.build):
  P0 never fetched / last failed -> due now;  P1 uncertain -> TTL ttl_uncertain_h;  P2 -> TTL ttl_stable_h.
  A target fetched before but never classified by a build (no state yet) is stored as P1 with an empty
  ``last_state``, so a stale cache (older than ttl_uncertain_h) is due.

Execution (6.3): single queue, global token bucket (cfg.fetch.rate_per_sec); 429 -> halve rate + backoff,
recover x1.25 per 20 successes; 401/403 -> the client re-logs in once, a second refusal stops the run;
``max_consecutive_failures`` -> stop. Every result is upserted + logged immediately, so re-running resumes
(fetched targets are no longer due). Progress + ETA via ``progress`` (default: a log line every ~5 s).
``--dry-run``: build the FetchPlan and print it; ``est_requests`` equals what a real run sends (one request per
item, page 1 only; 429 retries excepted).
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

from .models import Priority, TargetKind, TargetState

if TYPE_CHECKING:
    from .config import Config
    from .fr24_client import FR24Client
    from .fr24_store import FR24Store
    from .models import PlanData
    from .vamsys import VamsysSnapshot

ProgressFn = Callable[[int, int, str], None]      # (done, total, current_query)
LogFn = Callable[[str], None]

SEARCH_KIND = "search"                            # FetchItem.kind for --search queries (not a TargetKind)
_PRIO_RANK = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2}
_KIND_RANK = {"flight": 0, "reg": 1, "board": 2, SEARCH_KIND: 3}
RATE_LIMIT_RETRIES = 3
META_LAST_DEEP = "last_deep_scan_at"


@dataclass(slots=True)
class FetchItem:
    query: str
    kind: TargetKind
    priority: Priority
    reason: str = ""                 # why due: 'new' | 'failed' | 'ttl_p1' | 'ttl_p2' | 'forced' | 'deep' | 'cao' | 'search'


@dataclass(slots=True)
class FetchPlan:
    items: list[FetchItem] = field(default_factory=list)
    skipped_fresh: int = 0           # targets not due (within TTL)
    deep: bool = False               # all registrations scheduled (sets meta last_deep_scan_at when completed)

    @property
    def est_requests(self) -> int:
        """One request per item (page 1 only)."""
        return len(self.items)

    def by_priority(self) -> dict[str, int]:
        c = Counter(str(i.priority) for i in self.items)
        return {k: c[k] for k in sorted(c)}

    def by_kind(self) -> dict[str, int]:
        c = Counter(str(i.kind) for i in self.items)
        return {k: c[k] for k in sorted(c, key=lambda k: _KIND_RANK.get(k, 9))}

    def by_reason(self) -> dict[str, int]:
        c = Counter(i.reason for i in self.items)
        return dict(c.most_common())

    def by_kind_priority(self) -> dict[str, dict[str, int]]:
        out: dict[str, Counter] = {}
        for i in self.items:
            out.setdefault(str(i.kind), Counter())[str(i.priority)] += 1
        return {k: dict(sorted(v.items())) for k, v in sorted(out.items(), key=lambda kv: _KIND_RANK.get(kv[0], 9))}


@dataclass(slots=True)
class FetchRunReport:
    planned: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    new_regs: list[str] = field(default_factory=list)
    new_numbers: list[str] = field(default_factory=list)
    stopped_early: bool = False
    stop_reason: str = ""
    elapsed_s: float = 0.0
    dry_run: bool = False
    requests: int = 0                # HTTP requests attempted (incl. 429 retries)
    rate_limited: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)   # (kind, query, error)


# ------------------------------------------------------------------ token bucket -------------------------------------
class TokenBucket:
    """Global rate limiter with adaptive backoff (halve on 429, recover x1.25 per N successes up to base)."""

    def __init__(self, rate_per_sec: float, *, burst: int = 1, min_rate: float = 0.1, recover_every: int = 20,
                 backoff_s: float = 15.0, max_backoff_s: float = 120.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.base_rate = float(rate_per_sec)
        self._rate = float(rate_per_sec)
        self.burst = max(1, int(burst))
        self.min_rate = min(min_rate, self.base_rate)
        self.recover_every = recover_every
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.burst)
        self._last = clock()
        self._blocked_until = 0.0
        self._successes = 0
        self._consecutive_429 = 0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._last) * self._rate)
        self._last = now

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            now = self._clock()
            if now < self._blocked_until:
                self._sleep(self._blocked_until - now)
                continue
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            self._sleep((1.0 - self._tokens) / self._rate)

    def on_rate_limited(self, retry_after: float | None = None) -> None:
        self._consecutive_429 += 1
        self._successes = 0
        self._rate = max(self.min_rate, self._rate / 2.0)
        wait = retry_after if retry_after and retry_after > 0 else self.backoff_s * self._consecutive_429
        self._blocked_until = self._clock() + min(wait, self.max_backoff_s)
        self._tokens = 0.0

    def on_success(self) -> None:
        self._consecutive_429 = 0
        if self._rate >= self.base_rate:
            return
        self._successes += 1
        if self._successes >= self.recover_every:
            self._successes = 0
            self._rate = min(self.base_rate, self._rate * 1.25)

    @property
    def rate(self) -> float:
        return self._rate


# ------------------------------------------------------------------ targets --------------------------------------------
def _row_ts(r: dict) -> int | None:
    v = r.get("real_dep") or r.get("sched_dep") or r.get("real_arr") or r.get("sched_arr")
    return int(v) if v else None


def _row_target_op(r: dict, ops: tuple[str, ...]) -> str | None:
    """Target operator of a cache row: callsign prefix; without a callsign, airline ICAO."""
    cs = str(r.get("callsign") or "").strip().upper()
    if cs:
        return cs[:3] if cs.startswith(ops) else None
    al = str(r.get("airline_icao") or "").strip().upper()
    return al if al in ops else None


def _is_actual(r: dict) -> bool:
    try:
        from .fr24_obs import is_actual_row
    except Exception:  # noqa: BLE001 - fr24_obs is being edited in parallel; same rule inline
        return bool(r.get("real_dep") or r.get("real_arr") or r.get("status_live")
                    or str(r.get("status_generic") or "").lower() in ("landed", "departed"))
    return is_actual_row(r)


@dataclass(slots=True)
class CacheDiscovery:
    numbers: set[str] = field(default_factory=set)              # target-op numbers seen since season start
    reg_last: dict[str, int] = field(default_factory=dict)      # reg -> latest actual target-op flight ts
    reg_ops: dict[str, set[str]] = field(default_factory=dict)  # reg -> target ops seen flying it


def discover_from_rows(cfg: "Config", rows: Iterable[dict]) -> CacheDiscovery:
    """Numbers and registrations of target operators in cache rows since season start."""
    ops = tuple(cfg.target_ops)
    prefix = cfg.number_prefix
    start = cfg.season_start_ts
    d = CacheDiscovery()
    for r in rows:
        ts = _row_ts(r)
        if not ts or ts < start:
            continue
        op = _row_target_op(r, ops)
        if not op:
            continue
        num = str(r.get("number") or "").strip().upper()
        if num.startswith(prefix) and len(num) >= 3:
            d.numbers.add(num)
        reg = str(r.get("registration") or "").strip().upper()
        if reg and _is_actual(r):
            if ts > d.reg_last.get(reg, 0):
                d.reg_last[reg] = ts
            d.reg_ops.setdefault(reg, set()).add(op)
    return d


def derive_targets(cfg: "Config", plan: "PlanData", vamsys: "VamsysSnapshot", store: "FR24Store",
                   *, now: int | None = None) -> list[TargetState]:
    """All flight + reg targets per plan 6.1, merged with existing ``targets`` rows (priority/last_ok kept).
    New targets get Priority.P0 — or P1 with no state when the cache already holds a successful fetch of
    them (legacy import / earlier runs), so their TTL applies. Does not write to the store."""
    now = int(now if now is not None else time.time())
    ops = tuple(cfg.target_ops)

    flight_reason: dict[str, str] = {}
    for n in sorted(plan.numbers):
        n = str(n).strip().upper()
        if n:
            flight_reason.setdefault(n, "plan")
    for r in vamsys.commercial_routes:
        if r.op not in ops:
            continue
        n = (r.number or cfg.callsign_to_number(r.callsign)).strip().upper()
        if n:
            flight_reason.setdefault(n, "vamsys")
    disc = discover_from_rows(cfg, store.iter_rows(since_ts=cfg.season_start_ts))
    for n in sorted(disc.numbers):
        flight_reason.setdefault(n, "discovered")

    cargo = set(cfg.cargo_ops)
    drop_before = now - cfg.fetch.reg_drop_after_days * 86400
    reg_reason: dict[str, str] = {}
    for reg, last in sorted(disc.reg_last.items()):
        if last < drop_before:
            continue
        reg_reason[reg] = "cao" if disc.reg_ops.get(reg, set()) & cargo else "fleet"

    existing = {(t.query, str(t.kind)): t for t in store.iter_targets()}
    fetched = store.last_fetches()
    out: list[TargetState] = []
    for kind, reasons in ((TargetKind.FLIGHT, flight_reason), (TargetKind.REG, reg_reason)):
        for q, reason in reasons.items():
            old = existing.get((q, str(kind)))
            if old is not None:
                out.append(TargetState(query=q, kind=kind, priority=old.priority, last_ok_at=old.last_ok_at,
                                       last_state=old.last_state,
                                       reason=reason if kind == TargetKind.REG or not old.reason else old.reason))
                continue
            lf = fetched.get((q, str(kind)))
            last_ok = lf["last_ok_at"] if lf else None
            failed = bool(lf and lf.get("error"))
            pr = Priority.P1 if (last_ok and not failed) else Priority.P0
            out.append(TargetState(query=q, kind=kind, priority=pr, last_ok_at=last_ok, last_state="", reason=reason))
    return out


def stale_reg_targets(store: "FR24Store", derived: Sequence[TargetState]) -> list[tuple[str, TargetKind]]:
    """Registration targets in the store that are no longer derived (unseen > reg_drop_after_days)."""
    keep = {t.query for t in derived if str(t.kind) == "reg"}
    return [(t.query, TargetKind.REG) for t in store.iter_targets(TargetKind.REG) if t.query not in keep]


def search_prefixes(numbers: Iterable[str], *, width: int = 5) -> list[str]:
    """As the former fetch_search_index.py script: one query per distinct 5-char prefix of the flight numbers."""
    return sorted({str(n).strip().upper()[:width] for n in numbers if len(str(n).strip()) >= 3})


def plan_fetch(cfg: "Config", targets: Sequence[TargetState], *, now: int | None = None, deep: bool = False,
               flights: Iterable[str] | None = None, regs: Iterable[str] | None = None,
               boards: Iterable[str] | None = None, search: bool = False,
               last_deep_scan_at: int | None = None, cao_regs: Iterable[str] | None = None,
               search_queries: Iterable[str] | None = None) -> FetchPlan:
    """Select due targets (tiers + TTL, see module doc). Explicit ``flights``/``regs``/``boards`` are forced
    regardless of TTL and restrict the run to them. Order: P0, then P1, then P2; flights before regs."""
    now = int(now if now is not None else time.time())
    fs = cfg.fetch
    fp = FetchPlan()
    items: list[FetchItem] = []
    explicit = any(x for x in (flights, regs, boards))

    if explicit:
        seen = set()
        for kind, qs in ((TargetKind.FLIGHT, flights), (TargetKind.REG, regs), (TargetKind.BOARD, boards)):
            for q in qs or ():
                q = str(q).strip().upper()
                if q and (q, kind) not in seen:
                    seen.add((q, kind))
                    items.append(FetchItem(q, kind, Priority.P0, "forced"))
    else:
        ttl = {Priority.P1: fs.ttl_uncertain_h * 3600, Priority.P2: fs.ttl_stable_h * 3600}
        deep_due = bool(deep) or last_deep_scan_at is None or now - int(last_deep_scan_at) >= fs.deep_scan_every_days * 86400
        fp.deep = deep_due
        cao = {str(r).upper() for r in cao_regs} if cao_regs is not None else None
        for t in targets:
            kind = str(t.kind)
            if kind not in ("flight", "reg"):
                continue
            pr = t.priority if t.priority in _PRIO_RANK else Priority.P0
            if pr == Priority.P0 or not t.last_ok_at:
                items.append(FetchItem(t.query, t.kind, Priority.P0, "failed" if t.last_ok_at else "new"))
                continue
            if kind == "flight":
                if now - int(t.last_ok_at) >= ttl[pr]:
                    items.append(FetchItem(t.query, t.kind, pr, "ttl_p1" if pr == Priority.P1 else "ttl_p2"))
                else:
                    fp.skipped_fresh += 1
            else:
                is_cao = (t.query in cao) if cao is not None else (t.reason == "cao")
                if deep_due:
                    items.append(FetchItem(t.query, t.kind, pr, "deep"))
                elif is_cao:
                    items.append(FetchItem(t.query, t.kind, pr, "cao"))
                else:
                    fp.skipped_fresh += 1

    if search:
        qs = list(search_queries) if search_queries is not None else search_prefixes(
            t.query for t in targets if str(t.kind) == "flight")
        for q in qs:
            items.append(FetchItem(str(q).upper(), SEARCH_KIND, Priority.P2, "search"))  # type: ignore[arg-type]

    items.sort(key=lambda i: (_KIND_RANK.get(str(i.kind), 9) >= 2, _PRIO_RANK.get(i.priority, 3),
                              _KIND_RANK.get(str(i.kind), 9), i.query))
    fp.items = items
    return fp


# ------------------------------------------------------------------ execution ----------------------------------------
class _Progress:
    """Default progress: a log line every ``every_s`` seconds and at the end, with rate and ETA."""

    def __init__(self, log: LogFn, every_s: float = 5.0):
        self.log, self.every_s = log, every_s
        self.t0 = time.monotonic()
        self.last = 0.0

    def __call__(self, done: int, total: int, query: str) -> None:
        now = time.monotonic()
        if done < total and now - self.last < self.every_s:
            return
        self.last = now
        el = now - self.t0
        eta = (el / done) * (total - done) if done else 0.0
        self.log(f"  [{done}/{total}] {query:<10} 已用 {_fmt_s(el)}  预计剩余 {_fmt_s(eta)}")


def _fmt_s(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def run_fetch(cfg: "Config", store: "FR24Store", client: "FR24Client", fetch_plan: FetchPlan, *,
              progress: ProgressFn | None = None, log: LogFn = print,
              bucket: TokenBucket | None = None, now_fn: Callable[[], float] = time.time) -> FetchRunReport:
    """Execute the plan: rate-limit, fetch, upsert rows, log_fetch, update targets.last_ok_at; stop after
    cfg.fetch.max_consecutive_failures consecutive failures. Safe to interrupt (Ctrl+C) and re-run."""
    from .fr24_client import FR24AuthError, FR24Error, FR24RateLimited

    rep = FetchRunReport(planned=len(fetch_plan.items))
    if not fetch_plan.items:
        return rep
    bucket = bucket or TokenBucket(cfg.fetch.rate_per_sec)
    progress = progress or _Progress(log)
    max_fail = max(1, int(cfg.fetch.max_consecutive_failures))
    known = {(t.query, str(t.kind)): t for t in store.iter_targets()}
    known_numbers = {q for (q, k) in known if k == "flight"} | {i.query for i in fetch_plan.items if str(i.kind) == "flight"}
    known_regs = {q for (q, k) in known if k == "reg"} | {i.query for i in fetch_plan.items if str(i.kind) == "reg"}
    new_numbers: set[str] = set()
    new_regs: set[str] = set()
    t0 = time.monotonic()
    consecutive = 0

    def call(item: FetchItem):
        kind = str(item.kind)
        if kind in ("flight", "reg"):
            return client.flight_list(item.query, kind)  # type: ignore[arg-type]
        if kind == "board":
            return client.airport_board(item.query, pages=1)
        if kind == SEARCH_KIND:
            return client.search(item.query)
        raise ValueError(f"unknown fetch kind {kind!r}")

    def update_target(item: FetchItem, ok_at: int | None, failed: bool) -> None:
        kind = str(item.kind)
        if kind == SEARCH_KIND:
            return
        old = known.get((item.query, kind)) or store.get_target(item.query, item.kind)
        if failed:
            t = TargetState(item.query, item.kind, Priority.P0, old.last_ok_at if old else None,
                            old.last_state if old else "", "failed")
        else:
            pr = old.priority if old and old.priority in (Priority.P1, Priority.P2) else Priority.P1
            t = TargetState(item.query, item.kind, pr, ok_at, old.last_state if old else "",
                            (old.reason if old and old.reason != "failed" else item.reason) or item.reason)
        store.upsert_targets([t])
        known[(item.query, kind)] = t

    try:
        for idx, item in enumerate(fetch_plan.items, 1):
            kind = str(item.kind)
            err: str | None = None
            status: int | None = None
            result = None
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                bucket.acquire()
                rep.requests += 1
                try:
                    result = call(item)
                    bucket.on_success()
                    break
                except FR24RateLimited as e:
                    rep.rate_limited += 1
                    bucket.on_rate_limited(e.retry_after)
                    err, status = str(e), 429
                    log(f"  429 限流: {item.query}，速率降至 {bucket.rate:.2f}/s 并退避")
                    continue
                except FR24AuthError as e:
                    err, status = str(e), e.http_status
                    rep.stopped_early, rep.stop_reason = True, f"auth: {e}"
                    break
                except FR24Error as e:
                    err, status = str(e), e.http_status
                    break
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:  # noqa: BLE001 - unexpected payload/library errors are per-item failures
                    err, status = f"{type(e).__name__}: {str(e)[:200]}", None
                    break
            at = int(now_fn())
            rep.done = idx
            if result is not None:
                consecutive = 0
                rep.ok += 1
                if kind == SEARCH_KIND:
                    store.upsert_search_payload(item.query, result, at)
                    store.log_fetch(item.query, kind, at, 200, len(result.get("schedule") or []), None)
                else:
                    rows = result.rows
                    st = store.upsert_rows(rows, source=f"{kind}:{item.query}", seen_at=result.fetched_at or at)
                    rep.rows_inserted += st.inserted
                    rep.rows_updated += st.updated
                    store.log_fetch(item.query, kind, result.fetched_at or at, result.http_status, len(rows), None)
                    update_target(item, result.fetched_at or at, failed=False)
                    disc = discover_from_rows(cfg, rows)
                    new_numbers |= {n for n in disc.numbers if n not in known_numbers}
                    new_regs |= {r for r in disc.reg_last if r not in known_regs}
            else:
                rep.failed += 1
                consecutive += 1
                rep.failures.append((kind, item.query, err or "?"))
                store.log_fetch(item.query, kind, at, status, 0, err or "unknown error")
                update_target(item, None, failed=True)
                log(f"  失败 {kind} {item.query}: {err}")
                if rep.stopped_early:
                    break
                if consecutive >= max_fail:
                    rep.stopped_early, rep.stop_reason = True, f"连续 {consecutive} 次失败"
                    break
            progress(idx, len(fetch_plan.items), item.query)
    except KeyboardInterrupt:
        rep.stopped_early, rep.stop_reason = True, "interrupted"
        log("  已中断：已抓取的结果均已保存，重新运行将从未完成的目标继续。")

    rep.new_numbers = sorted(new_numbers)
    rep.new_regs = sorted(new_regs)
    rep.elapsed_s = time.monotonic() - t0
    if fetch_plan.deep and not rep.stopped_early:
        store.set_meta(META_LAST_DEEP, str(int(now_fn())))
    store.set_meta("last_fetch_run_at", str(int(now_fn())))
    if rep.requests >= 50 and rep.elapsed_s > 0:
        store.set_meta(META_LAST_RPS, f"{rep.requests / rep.elapsed_s:.3f}")
    return rep


# ------------------------------------------------------------------ CLI entry ----------------------------------------
META_LAST_RPS = "last_fetch_rps"


def _reason_head(reason: str | None) -> str:
    """Targets written back by build carry composite reasons ('CREATE_FR24,recent_acts<min'); summarise by the
    first token so the plan printout stays one short line."""
    return (reason or "-").split(",")[0]


def _print_plan(fp: FetchPlan, targets: Sequence[TargetState], cfg: "Config", log: LogFn, *, sample: int = 15,
                measured_rps: float | None = None) -> None:
    tk = Counter(str(t.kind) for t in targets)
    tr = Counter((str(t.kind), _reason_head(t.reason)) for t in targets)
    tp = Counter((str(t.kind), str(t.priority)) for t in targets)

    def top(kind: str, n: int = 8) -> str:
        items = sorted(((r, c) for (k, r), c in tr.items() if k == kind), key=lambda x: -x[1])
        s = "，".join(f"{r} {c}" for r, c in items[:n])
        return s + (f"，其他 {sum(c for _, c in items[n:])}" if len(items) > n else "")

    log(f"目标: 班号 {tk.get('flight', 0)}（{top('flight')}）  注册号 {tk.get('reg', 0)}（{top('reg')}）")
    log("目标级别: " + "  ".join(f"{k}/{p} {n}" for (k, p), n in sorted(tp.items())))
    log(f"本次将抓取 {fp.est_requests} 个请求（未到期跳过 {fp.skipped_fresh}）"
        f"{'，含全机队注册号扫描' if fp.deep else ''}")
    log("  按类别/级别: " + "  ".join(f"{k}: {v}" for k, v in fp.by_kind_priority().items()))
    log("  按原因: " + "  ".join(f"{k} {v}" for k, v in fp.by_reason().items()))
    if fp.est_requests:
        rps = min(cfg.fetch.rate_per_sec, measured_rps) if measured_rps else cfg.fetch.rate_per_sec
        basis = f"按上次实测 {measured_rps:.2f} 次/秒" if measured_rps else f"按限速 {cfg.fetch.rate_per_sec:g} 次/秒"
        log(f"  预计耗时 ≈ {_fmt_s(fp.est_requests / rps)}（{basis}）")
        head = fp.items[:sample]
        log("  前 %d 项: " % len(head) + " ".join(f"{i.kind}:{i.query}({i.priority}/{i.reason})" for i in head))


def fetch(cfg: "Config", *, deep: bool = False, flights: Iterable[str] | None = None,
          regs: Iterable[str] | None = None, boards: Iterable[str] | None = None, search: bool = False,
          dry_run: bool = False, log: LogFn = print, client: "FR24Client | None" = None,
          now: int | None = None) -> FetchRunReport:
    """CLI entry for ``vcca_plan.py fetch``: open store (created on first run), load plan + VAMSYS,
    derive targets, plan, (dry-run: print and return), log in, run. ``client`` may be injected (tests)."""
    from . import caac, vamsys
    from .fr24_store import FR24Store

    flights = list(flights) if flights else None
    regs = list(regs) if regs else None
    boards = list(boards) if boards else None
    with FR24Store.open(cfg) as store:
        plan = caac.load_plan(cfg)
        vs = vamsys.load_vamsys(cfg)
        targets = derive_targets(cfg, plan, vs, store, now=now)
        last_deep = store.get_meta(META_LAST_DEEP)
        fp = plan_fetch(cfg, targets, now=now, deep=deep, flights=flights, regs=regs, boards=boards, search=search,
                        last_deep_scan_at=int(last_deep) if last_deep else None,
                        cao_regs=[t.query for t in targets if str(t.kind) == "reg" and t.reason == "cao"])
        try:
            measured = float(store.get_meta(META_LAST_RPS) or 0) or None
        except ValueError:
            measured = None
        _print_plan(fp, targets, cfg, log, measured_rps=measured)
        if dry_run:
            log("（--dry-run：未发送任何请求）")
            return FetchRunReport(planned=fp.est_requests, dry_run=True)

        store.upsert_targets(targets)
        stale = stale_reg_targets(store, targets)
        if stale:
            store.delete_targets(stale)
            log(f"移出 {len(stale)} 个超过 {cfg.fetch.reg_drop_after_days} 天未见的注册号目标")
        if not fp.items:
            log("没有到期的目标。")
            return FetchRunReport()

        own_client = client is None
        if own_client:
            from .fr24_client import FR24AuthError, FR24Client

            client = FR24Client(cfg)
            try:
                client.login()
            except FR24AuthError as e:
                log(f"登录失败: {e}")
                return FetchRunReport(planned=fp.est_requests, stopped_early=True, stop_reason=f"auth: {e}")
            log(f"FR24 会话就绪（{'缓存会话' if client.session_source == 'cache' else '新登录'}）")
        try:
            rep = run_fetch(cfg, store, client, fp, log=log)
        finally:
            if own_client:
                client.close()
        log(f"完成 {rep.done}/{rep.planned}：成功 {rep.ok}  失败 {rep.failed}  请求 {rep.requests}"
            f"（429 {rep.rate_limited}）  新增行 {rep.rows_inserted}  更新行 {rep.rows_updated}  "
            f"用时 {_fmt_s(rep.elapsed_s)}")
        if rep.new_numbers:
            log(f"发现计划外班号 {len(rep.new_numbers)}: {' '.join(rep.new_numbers[:30])}")
        if rep.new_regs:
            log(f"发现新注册号 {len(rep.new_regs)}: {' '.join(rep.new_regs[:30])}")
        if rep.stopped_early:
            log(f"提前停止: {rep.stop_reason}（进度已保存，重新运行即可续抓）")
        return rep
