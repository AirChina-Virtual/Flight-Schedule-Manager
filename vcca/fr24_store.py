"""SQLite FR24 cache ``$VCCA_HOME/cache/fr24_cache.db`` (plan 6.4; step 3) — the only FR24 cache.

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
  meta(key PRIMARY KEY, value)         -- schema_version, last_deep_scan_at, ...

Merge rule (``upsert_rows``): a row seen at ``seen_at >= last_seen`` overwrites every column with its non-null
values (and takes over ``source``); an older observation only fills NULL columns. ``first_seen`` = min,
``last_seen`` = max. Empty strings are stored as NULL.

Row dicts returned by ``iter_rows`` use the ``fr24_client.FlightRow`` keys (``id`` = fr24_id) plus
``first_seen``, ``last_seen``, ``source``; absent strings/timestamps are ``None``,
``status_live`` is bool/None.
"""

from __future__ import annotations

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
        queries exactly like the former fetch_search_index.py script, or {} when nothing is stored."""
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

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "FR24Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


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
