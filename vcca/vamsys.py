"""Read-only loader for the FlightOpsDesk VAMSYS workspace (``Data/workspaces.db``).

The DB is opened with ``file:...?mode=ro`` — this module never writes. Tables used:
  workspaces(id, name, json)                  json.StorageRevision
  workspace_revisions(workspace_id, revision)
  resource_rows(workspace_id, resource, collection, local_id, ordinal, json)
      resource 0 = airports, 1 = fleets, 2 = aircraft, 3 = routings (route strings), 4 = routes
      json = {"LocalId", "Identity", "RawApiJson" (str), "Fields" (dict of str)}
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .models import RouteKey, VRoute
from .util_time import fmt_hm, parse_hhmm, vdays_to_days

if TYPE_CHECKING:
    from .config import Config

RES_AIRPORTS, RES_FLEETS, RES_AIRCRAFT, RES_ROUTINGS, RES_ROUTES = 0, 1, 2, 3, 4
SPECIAL_TYPES = ("training", "repositioning")
SPECIAL_TAGS = ("MIL", "TRAINING")


@dataclass(slots=True)
class VAirport:
    code: str                  # Fields['ICAO/IATA'] (the code routes use)
    icao: str                  # RawApiJson icao ('' if absent)
    iata: str
    name: str
    fields: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class VFleet:
    id: str
    name: str
    type_code: str             # 'A319'
    kind: str                  # 'pax' | 'cargo'
    fields: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """Legacy fleet_codes_str element: 'A332' or 'B77F(C)' for cargo fleets."""
        return self.type_code + ("(C)" if self.kind == "cargo" else "")


@dataclass(slots=True)
class VAircraft:
    id: str
    registration: str
    fleet_id: str
    name: str


@dataclass(slots=True)
class VamsysSnapshot:
    workspace_id: str
    workspace_name: str
    revision: int | None               # workspace_revisions.revision
    storage_revision: int | None       # workspaces.json StorageRevision
    db_path: Path
    airports: dict[str, VAirport]      # by Fields 'ICAO/IATA' (upper)
    fleets: dict[str, VFleet]          # by ID
    aircraft: list[VAircraft]
    routes: list[VRoute]
    routings: list[dict] = field(default_factory=list, repr=False)  # resource 3 Fields (unused by rules)

    # ---- views
    @property
    def commercial_routes(self) -> list[VRoute]:
        return [r for r in self.routes if not r.is_special]

    @property
    def special_routes(self) -> list[VRoute]:
        return [r for r in self.routes if r.is_special]

    def routes_by_key(self, commercial_only: bool = True) -> dict[RouteKey, list[VRoute]]:
        out: dict[RouteKey, list[VRoute]] = defaultdict(list)
        for r in (self.commercial_routes if commercial_only else self.routes):
            out[r.key].append(r)
        return dict(out)

    def route_by_id(self) -> dict[str, VRoute]:
        return {r.id: r for r in self.routes}

    def has_airport(self, code: str) -> bool:
        c = str(code or "").strip().upper()
        return c in self.airports or c in self._alt_codes()

    def _alt_codes(self) -> set[str]:
        return {a.icao for a in self.airports.values() if a.icao} | {a.iata for a in self.airports.values() if a.iata}

    def fleet_labels(self, fids: Iterable[str]) -> str:
        """Legacy fleet_codes_str: sorted by type code, cargo suffixed '(C)', unknown IDs as-is."""
        def lab(fid):
            f = self.fleets.get(fid)
            return (f.type_code if f else fid, fid, f.label if f else fid)
        return ",".join(x[2] for x in sorted(lab(fid) for fid in fids))

    def registrations_by_fleet(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for a in self.aircraft:
            out[a.fleet_id].append(a.registration)
        return dict(out)

    def counts(self) -> dict[str, Any]:
        by_op: dict[str, int] = defaultdict(int)
        for r in self.routes:
            by_op[r.op] += 1
        return {
            "airports": len(self.airports), "fleets": len(self.fleets), "aircraft": len(self.aircraft),
            "routings": len(self.routings), "routes": len(self.routes),
            "commercial_routes": len(self.commercial_routes), "special_routes": len(self.special_routes),
            "routes_with_end_date": sum(1 for r in self.routes if r.has_end_date),
            "routes_by_op": dict(sorted(by_op.items(), key=lambda kv: -kv[1])),
        }


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the workspace DB strictly read-only."""
    p = Path(db_path)
    if not p.is_file():
        raise FileNotFoundError(f"VAMSYS 数据库不存在: {p}")
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def _rows(cur: sqlite3.Cursor, ws: str, resource: int, collection: int) -> list[dict]:
    q = "SELECT json FROM resource_rows WHERE workspace_id=? AND resource=? AND collection=? ORDER BY ordinal"
    return [json.loads(j) for (j,) in cur.execute(q, (ws, resource, collection))]


def _raw(row: dict) -> dict:
    try:
        return json.loads(row.get("RawApiJson") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def parse_route(row: dict, target_ops: Sequence[str]) -> VRoute:
    """resource_rows json -> VRoute (legacy load_vamsys_db field derivation)."""
    f: dict[str, str] = row["Fields"]
    raw = _raw(row)
    cs = str(f.get("Callsign") or "").strip().upper()
    op = cs[:3] if len(cs) >= 3 and cs[:3].isalpha() else cs
    dep_min = parse_hhmm(f.get("Departure Time (HH:MM)"))
    arr_min = parse_hhmm(f.get("Arrival Time (HH:MM)"))
    rtype = str(f.get("Type") or "").strip()
    tags = str(f.get("Tags") or "")
    reasons = []
    if rtype in SPECIAL_TYPES:
        reasons.append(f"type={rtype}")
    if op not in target_ops:
        reasons.append(f"op={op}")
    hit_tags = [t for t in SPECIAL_TAGS if t in tags]
    if hit_tags:
        reasons.append("tag=" + "/".join(hit_tags))
    return VRoute(
        id=str(f.get("ID") or "").strip(),
        op=op,
        callsign=cs,
        number=str(f.get("Flight Number") or "").strip().upper(),
        orig=str(f.get("Departure Airport (ICAO/IATA)") or "").strip().upper(),
        dest=str(f.get("Arrival Airport (ICAO/IATA)") or "").strip().upper(),
        type=rtype,
        dep=fmt_hm(dep_min) if dep_min is not None else "",
        arr=fmt_hm(arr_min) if arr_min is not None else "",
        dep_min=dep_min,
        arr_min=arr_min,
        days=vdays_to_days(f.get("Service Days")),
        fleet_ids=frozenset(x.strip() for x in str(f.get("Fleet IDs") or "").split(",") if x.strip()),
        start_date=str(f.get("Start Date") or "").strip(),
        end_date=str(f.get("End Date") or "").strip(),
        cost_index=str(f.get("Cost Index") or "").strip(),
        hidden=str(f.get("Is Hidden") or "").strip().upper() == "TRUE",
        tags=tags,
        is_special=bool(reasons),
        special_reason=";".join(reasons),
        fields=f,
        raw=raw,
    )


def load_vamsys(cfg: "Config" = None, *, db_path: Path | None = None, workspace_id: str | None = None,
                collection: int | None = None, target_ops: Sequence[str] | None = None) -> VamsysSnapshot:
    """Load airports, fleets, aircraft, routings and routes. Explicit kwargs override ``cfg``."""
    db_path = Path(db_path or cfg.vamsys_db)
    ws = workspace_id or cfg.workspace_id
    coll = collection if collection is not None else cfg.collection
    ops = tuple(target_ops or cfg.target_ops)
    conn = connect_ro(db_path)
    try:
        cur = conn.cursor()
        r = cur.execute("SELECT revision FROM workspace_revisions WHERE workspace_id=?", (ws,)).fetchone()
        rev = int(r[0]) if r else None
        w = cur.execute("SELECT name, json FROM workspaces WHERE id=?", (ws,)).fetchone()
        if w is None:
            raise LookupError(f"VAMSYS 数据库中没有 workspace {ws}")
        ws_name = w[0]
        try:
            storage_rev = json.loads(w[1]).get("StorageRevision")
        except json.JSONDecodeError:
            storage_rev = None

        airports = {}
        for row in _rows(cur, ws, RES_AIRPORTS, coll):
            f = row["Fields"]
            raw = _raw(row)
            code = str(f.get("ICAO/IATA") or "").strip().upper()
            airports[code] = VAirport(code=code, icao=str(raw.get("icao") or "").upper(),
                                      iata=str(raw.get("iata") or "").upper(), name=str(f.get("Name") or ""), fields=f)
        fleets = {}
        for row in _rows(cur, ws, RES_FLEETS, coll):
            f = row["Fields"]
            fid = str(f.get("ID") or "").strip()
            fleets[fid] = VFleet(id=fid, name=str(f.get("Name") or ""), type_code=str(f.get("Type Code") or ""),
                                 kind=str(f.get("Type (pax/cargo/...)") or ""), fields=f)
        aircraft = [VAircraft(id=str(f.get("ID") or ""), registration=str(f.get("Registration") or "").strip().upper(),
                              fleet_id=str(f.get("Fleet ID") or "").strip(), name=str(f.get("Name") or ""))
                    for f in (row["Fields"] for row in _rows(cur, ws, RES_AIRCRAFT, coll))]
        routings = [row["Fields"] for row in _rows(cur, ws, RES_ROUTINGS, coll)]
        routes = [parse_route(row, ops) for row in _rows(cur, ws, RES_ROUTES, coll)]
    finally:
        conn.close()
    return VamsysSnapshot(workspace_id=ws, workspace_name=ws_name, revision=rev, storage_revision=storage_rev,
                          db_path=db_path, airports=airports, fleets=fleets, aircraft=aircraft, routes=routes,
                          routings=routings)


def read_revision(cfg: "Config") -> tuple[int | None, int | None]:
    """Cheap (revision, storage_revision) read for status / staleness checks."""
    conn = connect_ro(cfg.vamsys_db)
    try:
        r = conn.execute("SELECT revision FROM workspace_revisions WHERE workspace_id=?", (cfg.workspace_id,)).fetchone()
        w = conn.execute("SELECT json FROM workspaces WHERE id=?", (cfg.workspace_id,)).fetchone()
        return (int(r[0]) if r else None), (json.loads(w[0]).get("StorageRevision") if w else None)
    finally:
        conn.close()


__all__ = ["VAirport", "VFleet", "VAircraft", "VamsysSnapshot", "load_vamsys", "parse_route", "read_revision",
           "connect_ro"]
