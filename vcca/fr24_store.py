"""SQLite FR24 cache ``fr24_cache/fr24_cache.db`` (plan 6.4; step 3).

Schema (SCHEMA_VERSION 1; created by ``FR24Store.init_schema``):

  flight_rows(fr24_id TEXT PRIMARY KEY, number, callsign, airline_icao, reg, ac_code,
              orig_icao, orig_iata, dest_icao, dest_iata,
              sched_dep INT, sched_arr INT, real_dep INT, real_arr INT,
              status_generic, status_live INT, status_text,
              first_seen INT, last_seen INT, source TEXT)
      -- upsert by fr24_id; rows without an FR24 id get a synthetic id
      --   'nid:' + sha1(callsign|number|orig|dest|sched_dep)[:16]   (see ``synthetic_id``)
      -- history accumulates: rows are never deleted by a fetch
  fetch_log(id INTEGER PRIMARY KEY, query, kind, fetched_at INT, http_status INT, rows INT, error TEXT)
  targets(query, kind, priority, last_ok_at INT, last_state, reason, PRIMARY KEY(query, kind))
  airport_info(icao PRIMARY KEY, iata, name)
  search_queries(query PRIMARY KEY, payload TEXT, fetched_at INT)   -- aux: search index (by_query)
  meta(key PRIMARY KEY, value)         -- schema_version, legacy_import_done_at, last_deep_scan_at, ...

Merge rule (``upsert_rows``): a row seen at ``seen_at >= last_seen`` overwrites every column with its non-null
values (and takes over ``source``); an older observation only fills NULL columns. ``first_seen`` = min,
``last_seen`` = max. Empty strings are stored as NULL.

Legacy import (first run, idempotent): flight_histories.json (source 'flight:<num>'), reg_histories.json
('reg:<reg>'), airports/*.json ('board:<icao>'), live_summary.json ('live'), search_index.json (search_queries
table). Each legacy payload also gets a fetch_log entry (so TTL planning knows when it was fetched). The JSON
files are only read, never written.

Row dicts returned by ``iter_rows`` use the ``fr24_client.FlightRow`` keys (``id`` = fr24_id) plus
``first_seen``, ``last_seen``, ``source``; absent strings/timestamps are ``None`` (same as legacy JSON rows),
``status_live`` is bool/None.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from .models import Priority, TargetKind, TargetState

if TYPE_CHECKING:
    from .config import Config

SCHEMA_VERSION = 1

# FlightRow key -> flight_rows column
ROW_COLS: dict[str, str] = {
    "number": "number", "callsign": "callsign", "airline_icao": "airline_icao", "registration": "reg",
    "aircraft_code": "ac_code", "origin_icao": "orig_icao", "origin_iata": "orig_iata",
    "dest_icao": "dest_icao", "dest_iata": "dest_iata",
    "sched_dep": "sched_dep", "sched_arr": "sched_arr", "real_dep": "real_dep", "real_arr": "real_arr",
    "status_generic": "status_generic", "status_live": "status_live", "status_text": "status_text",
}
_DATA_COLS = tuple(ROW_COLS.values())
_ALL_COLS = ("fr24_id",) + _DATA_COLS + ("first_seen", "last_seen", "source")
_UPPER_KEYS = {"number", "callsign", "airline_icao", "registration", "aircraft_code",
               "origin_icao", "origin_iata", "dest_icao", "dest_iata"}
_TS_KEYS = {"sched_dep", "sched_arr", "real_dep", "real_arr"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_rows(
    fr24_id TEXT PRIMARY KEY, number TEXT, callsign TEXT, airline_icao TEXT, reg TEXT, ac_code TEXT,
    orig_icao TEXT, orig_iata TEXT, dest_icao TEXT, dest_iata TEXT,
    sched_dep INTEGER, sched_arr INTEGER, real_dep INTEGER, real_arr INTEGER,
    status_generic TEXT, status_live INTEGER, status_text TEXT,
    first_seen INTEGER, last_seen INTEGER, source TEXT);
CREATE INDEX IF NOT EXISTS ix_rows_number ON flight_rows(number);
CREATE INDEX IF NOT EXISTS ix_rows_callsign ON flight_rows(callsign);
CREATE INDEX IF NOT EXISTS ix_rows_reg ON flight_rows(reg);
CREATE INDEX IF NOT EXISTS ix_rows_sched_dep ON flight_rows(sched_dep);
CREATE TABLE IF NOT EXISTS fetch_log(
    id INTEGER PRIMARY KEY, query TEXT, kind TEXT, fetched_at INTEGER, http_status INTEGER, rows INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS ix_fetch_log_q ON fetch_log(query, kind, fetched_at);
CREATE TABLE IF NOT EXISTS targets(
    query TEXT, kind TEXT, priority TEXT, last_ok_at INTEGER, last_state TEXT, reason TEXT,
    PRIMARY KEY(query, kind));
CREATE TABLE IF NOT EXISTS airport_info(icao TEXT PRIMARY KEY, iata TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS search_queries(query TEXT PRIMARY KEY, payload TEXT, fetched_at INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

_BATCH = 2000


@dataclass(slots=True)
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def add(self, o: "UpsertStats") -> None:
        self.inserted += o.inserted
        self.updated += o.updated
        self.unchanged += o.unchanged


@dataclass(slots=True)
class ImportReport:
    already_done: bool = False
    flights: int = 0                     # flight-number payloads read
    regs: int = 0
    boards: int = 0
    live: int = 0                        # live_summary rows read (live list + details + detail history)
    rows_in: int = 0                     # rows read from JSON
    rows_inserted: int = 0
    rows_updated: int = 0
    airports: int = 0                    # airports/*.json files read
    errors: list[str] = field(default_factory=list)
    # extra detail (step 3)
    rows_by_source: dict[str, int] = field(default_factory=dict)   # 'flight'|'reg'|'board'|'live' -> rows read
    rows_without_id: int = 0             # rows given a synthetic id
    duplicates: int = 0                  # rows_in - distinct ids across all sources
    distinct_ids: int = 0
    missing_after_import: int = 0        # distinct ids NOT found in flight_rows afterwards (must be 0)
    search_queries: int = 0
    search_by_flight: int = 0
    search_by_callsign: int = 0
    legacy_errors: int = 0               # error entries carried in the legacy files (logged to fetch_log)

    def lines(self) -> list[str]:
        if self.already_done:
            return ["旧 JSON 已导入过（跳过）"]
        return [
            f"旧 JSON 导入: 班号 {self.flights}  注册号 {self.regs}  看板 {self.boards}  实时 {self.live} 行  "
            f"搜索 {self.search_queries} 查询",
            f"  行读入 {self.rows_in} = " + " + ".join(f"{k} {v}" for k, v in self.rows_by_source.items())
            + f"；不同 id {self.distinct_ids}（重复 {self.duplicates}，无 id {self.rows_without_id}）",
            f"  新增 {self.rows_inserted}  更新 {self.rows_updated}  导入后缺失 {self.missing_after_import}"
            + (f"  错误 {len(self.errors)}" if self.errors else ""),
        ]


@dataclass(slots=True)
class StoreStats:
    rows: int = 0
    min_ts: int | None = None            # min(sched_dep/real_dep)
    max_ts: int | None = None
    last_fetch_at: int | None = None
    fetches_by_kind: dict[str, int] = field(default_factory=dict)
    targets_by_priority: dict[str, int] = field(default_factory=dict)
    stale_targets: int = 0               # targets whose TTL has expired (per cfg.fetch)


def _now() -> int:
    return int(time.time())


def _norm_value(key: str, v: Any) -> Any:
    if v is None:
        return None
    if key in _TS_KEYS:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return None
        return iv or None
    if key == "status_live":
        return 1 if v else 0
    s = str(v).strip()
    if not s:
        return None
    if key in _UPPER_KEYS:
        return s.upper()
    if key == "status_generic":
        return s.lower()
    return s


def _norm_row(row: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    """FlightRow -> (fr24_id, {column: value}, synthetic?)."""
    cols = {col: _norm_value(key, row.get(key)) for key, col in ROW_COLS.items()}
    rid = row.get("id")
    rid = str(rid).strip() if rid not in (None, "") else ""
    if rid:
        return rid, cols, False
    return synthetic_id(row), cols, True


def _iso_to_ts(s: Any) -> int | None:
    if not s:
        return None
    try:
        return int(_dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp())
    except ValueError:
        return None


def _db_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = {"id": r["fr24_id"]}
    for key, col in ROW_COLS.items():
        v = r[col]
        if key == "status_live" and v is not None:
            v = bool(v)
        d[key] = v
    d["first_seen"] = r["first_seen"]
    d["last_seen"] = r["last_seen"]
    d["source"] = r["source"]
    return d


class FR24Store:
    """Thin DAO over fr24_cache.db. Use as a context manager. ``readonly=True`` opens with mode=ro
    (status/build); fetch opens read-write."""

    def __init__(self, db_path: Path, *, readonly: bool = False):
        self.db_path = Path(db_path)
        self.readonly = readonly
        if readonly:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"fr24_cache.db 不存在: {self.db_path}")
            uri = "file:" + self.db_path.resolve().as_posix() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        if not readonly:
            self.init_schema()

    @classmethod
    def open(cls, cfg: "Config", *, readonly: bool = False) -> "FR24Store":
        """Open ``cfg.cache_db`` (creating schema when writable)."""
        return cls(cfg.cache_db, readonly=readonly)

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ---- transactions
    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    # ---- flight rows
    def upsert_rows(self, rows: Iterable[dict[str, Any]], *, source: str, seen_at: int) -> UpsertStats:
        """Insert/update FlightRows (keys per fr24_client.ROW_KEYS). Newer non-null values win; first_seen kept.
        One transaction per call (per fetch result), committed before returning."""
        stats = UpsertStats()
        batch: dict[str, dict[str, Any]] = {}
        self._begin()
        try:
            for row in rows:
                rid, cols, _ = _norm_row(row)
                prev = batch.get(rid)
                batch[rid] = cols if prev is None else {k: (cols[k] if cols[k] is not None else prev[k]) for k in cols}
                if len(batch) >= _BATCH:
                    stats.add(self._upsert_batch(batch, source, int(seen_at)))
                    batch = {}
            if batch:
                stats.add(self._upsert_batch(batch, source, int(seen_at)))
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return stats

    def _upsert_batch(self, batch: dict[str, dict[str, Any]], source: str, seen_at: int) -> UpsertStats:
        st = UpsertStats()
        ids = list(batch)
        existing: dict[str, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = f"SELECT * FROM flight_rows WHERE fr24_id IN ({','.join('?' * len(chunk))})"
            for r in self.conn.execute(q, chunk):
                existing[r["fr24_id"]] = r
        inserts, updates = [], []
        for rid, cols in batch.items():
            old = existing.get(rid)
            if old is None:
                inserts.append((rid, *[cols[c] for c in _DATA_COLS], seen_at, seen_at, source))
                st.inserted += 1
                continue
            newer = old["last_seen"] is None or seen_at >= old["last_seen"]
            merged = {}
            for c in _DATA_COLS:
                nv, ov = cols[c], old[c]
                merged[c] = (nv if nv is not None else ov) if newer else (ov if ov is not None else nv)
            changed = any(merged[c] != old[c] for c in _DATA_COLS)
            first = min(x for x in (old["first_seen"], seen_at) if x is not None)
            last = max(x for x in (old["last_seen"], seen_at) if x is not None)
            src = source if newer else old["source"]
            if changed or first != old["first_seen"] or last != old["last_seen"] or src != old["source"]:
                updates.append((*[merged[c] for c in _DATA_COLS], first, last, src, rid))
            if changed:
                st.updated += 1
            else:
                st.unchanged += 1
        if inserts:
            self.conn.executemany(
                f"INSERT INTO flight_rows({','.join(_ALL_COLS)}) VALUES ({','.join('?' * len(_ALL_COLS))})", inserts)
        if updates:
            sets = ",".join(f"{c}=?" for c in _DATA_COLS + ("first_seen", "last_seen", "source"))
            self.conn.executemany(f"UPDATE flight_rows SET {sets} WHERE fr24_id=?", updates)
        return st

    def iter_rows(self, *, number: str | None = None, callsign: str | None = None, reg: str | None = None,
                  since_ts: int | None = None) -> Iterator[dict[str, Any]]:
        """Rows filtered by any combination (AND). No filter = all rows. ``since_ts`` compares the event time
        coalesce(real_dep, sched_dep, real_arr, sched_arr)."""
        where, args = [], []
        for col, v in (("number", number), ("callsign", callsign), ("reg", reg)):
            if v:
                where.append(f"{col}=?")
                args.append(str(v).strip().upper())
        if since_ts is not None:
            where.append("coalesce(real_dep, sched_dep, real_arr, sched_arr) >= ?")
            args.append(int(since_ts))
        q = "SELECT * FROM flight_rows" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY fr24_id"
        for r in self.conn.execute(q, args):
            yield _db_row_to_dict(r)

    def count_rows(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM flight_rows").fetchone()[0])

    def has_row_ids(self, ids: Iterable[str]) -> set[str]:
        """Subset of ``ids`` present in flight_rows."""
        ids = list(ids)
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = f"SELECT fr24_id FROM flight_rows WHERE fr24_id IN ({','.join('?' * len(chunk))})"
            found.update(r[0] for r in self.conn.execute(q, chunk))
        return found

    # ---- fetch log
    def log_fetch(self, query: str, kind: str, fetched_at: int, http_status: int | None, rows: int,
                  error: str | None = None) -> None:
        self.conn.execute("INSERT INTO fetch_log(query, kind, fetched_at, http_status, rows, error) VALUES (?,?,?,?,?,?)",
                          (str(query), str(kind), int(fetched_at), http_status, int(rows), error))

    def last_fetch(self, query: str, kind: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM fetch_log WHERE query=? AND kind=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                              (str(query), str(kind))).fetchone()
        return dict(r) if r else None

    def last_fetches(self, kind: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        """(query, kind) -> latest fetch_log entry, plus 'last_ok_at' (latest successful fetch time or None)."""
        q = "SELECT * FROM fetch_log" + (" WHERE kind=?" if kind else "") + " ORDER BY fetched_at, id"
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for r in self.conn.execute(q, (str(kind),) if kind else ()):
            k = (r["query"], r["kind"])
            prev_ok = out[k]["last_ok_at"] if k in out else None
            d = dict(r)
            d["last_ok_at"] = r["fetched_at"] if r["error"] is None else prev_ok
            out[k] = d
        return out

    # ---- targets (plan 6.2)
    @staticmethod
    def _target(r: sqlite3.Row) -> TargetState:
        try:
            kind = TargetKind(r["kind"])
        except ValueError:
            kind = r["kind"]
        try:
            pr = Priority(r["priority"])
        except ValueError:
            pr = Priority.P0
        return TargetState(query=r["query"], kind=kind, priority=pr, last_ok_at=r["last_ok_at"],
                           last_state=r["last_state"] or "", reason=r["reason"] or "")

    def get_target(self, query: str, kind: TargetKind) -> TargetState | None:
        r = self.conn.execute("SELECT * FROM targets WHERE query=? AND kind=?", (str(query), str(kind))).fetchone()
        return self._target(r) if r else None

    def iter_targets(self, kind: TargetKind | None = None) -> Iterator[TargetState]:
        q = "SELECT * FROM targets" + (" WHERE kind=?" if kind else "") + " ORDER BY kind, query"
        for r in self.conn.execute(q, (str(kind),) if kind else ()):
            yield self._target(r)

    def upsert_targets(self, targets: Iterable[TargetState]) -> int:
        """Insert or update; returns count. Used by fr24_fetch (last_ok_at) and by build (priority/last_state).
        An incoming ``last_ok_at=None`` never erases a stored one (build passes tiers without fetch times);
        the other columns are replaced."""
        vals = [(t.query, str(t.kind), str(t.priority), t.last_ok_at, t.last_state or "", t.reason or "")
                for t in targets]
        if not vals:
            return 0
        self._begin()
        try:
            self.conn.executemany(
                "INSERT INTO targets(query, kind, priority, last_ok_at, last_state, reason) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(query, kind) DO UPDATE SET priority=excluded.priority, "
                "last_ok_at=coalesce(excluded.last_ok_at, targets.last_ok_at), "
                "last_state=excluded.last_state, reason=excluded.reason", vals)
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return len(vals)

    def delete_targets(self, keys: Iterable[tuple[str, TargetKind]]) -> int:
        """Drop targets (e.g. registrations unseen for reg_drop_after_days)."""
        keys = [(str(q), str(k)) for q, k in keys]
        if not keys:
            return 0
        self._begin()
        try:
            n = 0
            for q, k in keys:
                n += self.conn.execute("DELETE FROM targets WHERE query=? AND kind=?", (q, k)).rowcount
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return n

    # ---- airports
    def upsert_airport_info(self, icao: str, iata: str, name: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO airport_info(icao, iata, name) VALUES (?,?,?)",
                          (str(icao).strip().upper(), str(iata or "").strip().upper(), str(name or "").strip()))

    def airport_info(self) -> dict[str, tuple[str, str]]:
        """icao -> (iata, name)."""
        return {r["icao"]: (r["iata"] or "", r["name"] or "") for r in self.conn.execute("SELECT * FROM airport_info")}

    def iata_to_icao(self) -> dict[str, str]:
        """IATA -> ICAO built from airport_info and flight_rows (orig/dest pairs; most frequent pair wins)."""
        out: dict[str, str] = {}
        for r in self.conn.execute("SELECT icao, iata FROM airport_info WHERE iata IS NOT NULL AND iata != ''"):
            out[r["iata"]] = r["icao"]
        q = """SELECT iata, icao, sum(n) AS n FROM (
                 SELECT orig_iata AS iata, orig_icao AS icao, count(*) AS n FROM flight_rows
                   WHERE orig_iata IS NOT NULL AND orig_icao IS NOT NULL GROUP BY 1, 2
                 UNION ALL
                 SELECT dest_iata, dest_icao, count(*) FROM flight_rows
                   WHERE dest_iata IS NOT NULL AND dest_icao IS NOT NULL GROUP BY 1, 2)
               GROUP BY iata, icao ORDER BY n DESC, icao"""
        for r in self.conn.execute(q):
            out.setdefault(r["iata"], r["icao"])
        return out

    # ---- aux
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))

    def upsert_search_payload(self, query: str, payload: dict[str, Any], fetched_at: int) -> None:
        """Store one search result ({'schedule': [...], 'live': [...]}) under its query (legacy by_query)."""
        slim = {"schedule": payload.get("schedule") or [], "live": payload.get("live") or []}
        self.conn.execute("INSERT OR REPLACE INTO search_queries(query, payload, fetched_at) VALUES (?,?,?)",
                          (str(query).upper(), json.dumps(slim, ensure_ascii=False), int(fetched_at)))

    def search_index(self, target_ops: Iterable[str] = ("CCA", "CAO", "CCD", "BJN"),
                     number_prefixes: Iterable[str] = ("CA", "BJN", "CAO", "CCD")) -> dict[str, Any]:
        """Search index ({'by_flight': {...}, 'by_callsign': {...}, 'by_query': {...}}) rebuilt from the stored
        queries exactly like legacy fetch_search_index.save_all, or {} when nothing is stored."""
        ops = tuple(target_ops)
        prefixes = tuple(number_prefixes)
        by_query: dict[str, Any] = {}
        for r in self.conn.execute("SELECT query, payload FROM search_queries ORDER BY rowid"):
            by_query[r["query"]] = json.loads(r["payload"])
        if not by_query:
            return {}
        by_flight: dict[str, Any] = {}
        by_callsign: dict[str, Any] = {}
        for d in by_query.values():
            for item in d.get("schedule", []):
                det = item.get("detail") or {}
                op, cs = det.get("operator"), det.get("callsign")
                fl = det.get("flight") or item.get("id")
                if op in ops or str(fl).startswith(prefixes):
                    if fl:
                        by_flight[fl] = item
                    if cs:
                        by_callsign[cs] = item
        return {"by_flight": by_flight, "by_callsign": by_callsign, "by_query": by_query}

    def stats(self, cfg: "Config | None" = None) -> StoreStats:
        s = StoreStats()
        s.rows = self.count_rows()
        r = self.conn.execute("SELECT min(coalesce(real_dep, sched_dep)), max(coalesce(real_dep, sched_dep)) "
                              "FROM flight_rows").fetchone()
        s.min_ts, s.max_ts = r[0], r[1]
        s.last_fetch_at = self.conn.execute("SELECT max(fetched_at) FROM fetch_log WHERE error IS NULL").fetchone()[0]
        s.fetches_by_kind = {r[0]: r[1] for r in self.conn.execute("SELECT kind, count(*) FROM fetch_log GROUP BY kind")}
        s.targets_by_priority = {r[0]: r[1] for r in
                                 self.conn.execute("SELECT priority, count(*) FROM targets GROUP BY priority ORDER BY 1")}
        if cfg is not None:
            fs = cfg.fetch
            ttl = {"P1": fs.ttl_uncertain_h * 3600, "P2": fs.ttl_stable_h * 3600}
        else:
            ttl = {"P1": 24 * 3600, "P2": 168 * 3600}
        now = _now()
        stale = 0
        for t in self.conn.execute("SELECT priority, last_ok_at FROM targets"):
            if t[0] not in ttl or t[1] is None or now - int(t[1]) >= ttl[t[0]]:
                stale += 1
        s.stale_targets = stale
        return s

    # ---- legacy import
    def import_legacy_json(self, cache_dir: Path, *, force: bool = False) -> ImportReport:
        """One-time import of the legacy JSON caches (see module doc). Idempotent unless force=True
        (force re-runs the upserts; they are themselves idempotent and fetch_log entries are not duplicated)."""
        rep = ImportReport()
        if self.get_meta("legacy_import_done_at") and not force:
            rep.already_done = True
            return rep
        files = legacy_cache_files(Path(cache_dir))
        all_ids: set[str] = set()
        stats = UpsertStats()

        def ingest(rows: list[dict], source: str, seen_at: int, bucket: str) -> None:
            for r in rows:
                rid, _, synth = _norm_row(r)
                all_ids.add(rid)
                rep.rows_without_id += synth
            rep.rows_in += len(rows)
            rep.rows_by_source[bucket] = rep.rows_by_source.get(bucket, 0) + len(rows)
            stats.add(self.upsert_rows(rows, source=source, seen_at=seen_at))

        def log_once(query: str, kind: str, at: int, rows: int, error: str | None = None) -> None:
            if self.conn.execute("SELECT 1 FROM fetch_log WHERE query=? AND kind=? AND fetched_at=? AND "
                                 "coalesce(error,'')=coalesce(?, '')", (query, kind, at, error)).fetchone():
                return
            self.log_fetch(query, kind, at, 200 if error is None else None, rows, error)

        def load(path: Path) -> dict | None:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                rep.errors.append(f"{path.name}: {type(e).__name__}: {e}")
                return None

        # flight / reg histories: oldest payload first so newer values win naturally
        for key, kind in (("flight_histories", "flight"), ("reg_histories", "reg")):
            if key not in files:
                continue
            data = load(files[key])
            if not data:
                continue
            file_ts = _iso_to_ts(data.get("updated_at_utc")) or int(files[key].stat().st_mtime)
            payloads = data.get("flights") or {}
            refreshed = [t for t in (_iso_to_ts(p.get("refreshed_at_utc")) for p in payloads.values()) if t]
            default_ts = min(refreshed) if refreshed else file_ts
            items = sorted(((_iso_to_ts(p.get("refreshed_at_utc")) or default_ts, q, p) for q, p in payloads.items()),
                           key=lambda x: (x[0], x[1]))
            for at, q, p in items:
                q = str(q).strip().upper()
                rows = list(p.get("rows") or [])
                ingest(rows, f"{kind}:{q}", at, kind)
                self._begin()
                log_once(q, kind, at, len(rows))
                self._commit()
            if kind == "flight":
                rep.flights = len(payloads)
            else:
                rep.regs = len(payloads)
            self._begin()
            for err in data.get("errors") or []:
                if isinstance(err, (list, tuple)) and err:
                    rep.legacy_errors += 1
                    log_once(str(err[0]).upper(), kind, file_ts, 0, f"legacy: {str(err[1:])[:200]}")
            self._commit()

        # airport boards
        if "airports_dir" in files:
            for path in sorted(files["airports_dir"].glob("*.json")):
                data = load(path)
                if data is None:
                    continue
                icao = str(data.get("airport") or path.stem).upper()
                at = _iso_to_ts(data.get("fetched_at_utc")) or int(path.stat().st_mtime)
                rows = list(data.get("rows") or [])
                ingest(rows, f"board:{icao}", at, "board")
                self._begin()
                log_once(icao, "board", at, len(rows))
                self._commit()
                rep.airports += 1
                rep.boards += 1

        # live summary
        if "live_summary" in files:
            data = load(files["live_summary"])
            if data:
                at = _iso_to_ts(data.get("fetched_at_utc")) or int(files["live_summary"].stat().st_mtime)
                rows = _live_summary_rows(data)
                ingest(rows, "live", at, "live")
                rep.live = len(rows)

        # search index (aux table)
        if "search_index" in files:
            data = load(files["search_index"])
            if data:
                at = _iso_to_ts(data.get("fetched_at_utc")) or int(files["search_index"].stat().st_mtime)
                self._begin()
                try:
                    for q, payload in (data.get("by_query") or {}).items():
                        self.upsert_search_payload(q, payload or {}, at)
                    self._commit()
                except BaseException:
                    self._rollback()
                    raise
                rep.search_queries = len(data.get("by_query") or {})
                idx = self.search_index()
                rep.search_by_flight = len(idx.get("by_flight", {}))
                rep.search_by_callsign = len(idx.get("by_callsign", {}))
                for name, legacy_n, n in (("by_flight", len(data.get("by_flight") or {}), rep.search_by_flight),
                                          ("by_callsign", len(data.get("by_callsign") or {}), rep.search_by_callsign)):
                    if legacy_n != n:
                        rep.errors.append(f"search_index {name}: legacy {legacy_n} != rebuilt {n}")

        rep.rows_inserted, rep.rows_updated = stats.inserted, stats.updated
        rep.distinct_ids = len(all_ids)
        rep.duplicates = rep.rows_in - rep.distinct_ids
        rep.missing_after_import = len(all_ids - self.has_row_ids(all_ids))
        if rep.missing_after_import:
            rep.errors.append(f"{rep.missing_after_import} ids missing after import")
        self.set_meta("legacy_import_done_at", str(_now()))
        self.set_meta("legacy_import_report", json.dumps(
            {k: getattr(rep, k) for k in ("flights", "regs", "boards", "live", "rows_in", "rows_inserted",
                                          "rows_updated", "airports", "rows_without_id", "duplicates",
                                          "distinct_ids", "missing_after_import", "search_queries")},
            ensure_ascii=False))
        return rep

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "FR24Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _live_summary_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """live_summary.json -> FlightRows: every live-list flight (from its detail when present, else the bare
    live entry without times, status_live=True), details not in the live list, and each detail's history."""
    details = data.get("details") or {}
    rows: list[dict[str, Any]] = []

    def from_detail(d: dict[str, Any], op: str, live: bool) -> dict[str, Any]:
        tm = d.get("time") or {}
        return {
            "id": d.get("id"), "number": d.get("number_default"), "callsign": d.get("callsign"),
            "airline_icao": d.get("airline_icao") or op, "origin_icao": d.get("origin_icao"),
            "origin_iata": d.get("origin_iata"), "dest_icao": d.get("dest_icao"), "dest_iata": d.get("dest_iata"),
            "aircraft_code": d.get("aircraft_code"), "registration": d.get("registration"),
            "sched_dep": (tm.get("scheduled") or {}).get("departure"),
            "sched_arr": (tm.get("scheduled") or {}).get("arrival"),
            "real_dep": (tm.get("real") or {}).get("departure"), "real_arr": (tm.get("real") or {}).get("arrival"),
            "status_live": True if live else None, "status_text": d.get("status_text"), "status_generic": None,
        }

    used = set()
    for op, fls in (data.get("live") or {}).items():
        for f in fls or []:
            fid = f.get("id")
            d = details.get(fid) if fid else None
            if d:
                rows.append(from_detail(d, op, True))
                used.add(fid)
            else:
                rows.append({
                    "id": fid, "number": f.get("number"), "callsign": f.get("callsign"),
                    "airline_icao": f.get("airline_icao") or op, "origin_icao": None,
                    "origin_iata": f.get("origin_iata"), "dest_icao": None, "dest_iata": f.get("destination_iata"),
                    "aircraft_code": f.get("aircraft_code"), "registration": f.get("registration"),
                    "sched_dep": None, "sched_arr": None, "real_dep": None, "real_arr": None,
                    "status_live": True, "status_text": None, "status_generic": None,
                })
    for fid, d in details.items():
        if fid not in used:
            rows.append(from_detail(d, d.get("operator_query") or "", False))
        for h in d.get("history") or []:
            tm = h.get("time") or {}
            rows.append({
                "id": h.get("id"), "number": h.get("number"), "callsign": None, "airline_icao": None,
                "origin_icao": h.get("origin_icao"), "origin_iata": h.get("origin_iata"),
                "dest_icao": h.get("dest_icao"), "dest_iata": h.get("dest_iata"),
                "aircraft_code": d.get("aircraft_code"), "registration": d.get("registration"),
                "sched_dep": (tm.get("scheduled") or {}).get("departure"),
                "sched_arr": (tm.get("scheduled") or {}).get("arrival"),
                "real_dep": (tm.get("real") or {}).get("departure"), "real_arr": (tm.get("real") or {}).get("arrival"),
                "status_live": None, "status_text": None, "status_generic": None,
            })
    return rows


def synthetic_id(row: dict[str, Any]) -> str:
    """Stable id for rows without an FR24 id: 'nid:' + sha1(callsign|number|orig|dest|sched_dep)[:16]."""
    def s(k: str) -> str:
        v = row.get(k)
        return str(v).strip().upper() if v not in (None, "") else ""

    orig = s("origin_icao") or s("origin_iata")
    dest = s("dest_icao") or s("dest_iata")
    sd = row.get("sched_dep")
    try:
        sd = str(int(sd)) if sd not in (None, "") else ""
    except (TypeError, ValueError):
        sd = ""
    key = "|".join((s("callsign"), s("number"), orig, dest, sd))
    return "nid:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def legacy_cache_files(cache_dir: Path) -> dict[str, Path]:
    """Existing legacy JSON cache files {'flight_histories': path, 'reg_histories': ..., 'live_summary': ...,
    'search_index': ..., 'airports_dir': ...} — only those that exist. Implemented (used by status)."""
    names = {
        "flight_histories": "flight_histories.json",
        "reg_histories": "reg_histories.json",
        "live_summary": "live_summary.json",
        "search_index": "search_index.json",
    }
    out = {k: cache_dir / v for k, v in names.items() if (cache_dir / v).is_file()}
    if (cache_dir / "airports").is_dir():
        out["airports_dir"] = cache_dir / "airports"
    return out
