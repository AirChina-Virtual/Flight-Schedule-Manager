"""Cut small real-data fixtures for tests/test_fr24_obs|evidence|reconcile.py from the legacy FR24 JSON cache,
the CAAC plan and the VAMSYS snapshot (all read-only). Re-run only when the samples should be refreshed:

    fr_venv\\Scripts\\python.exe tests\\fixtures\\reconcile\\make_fixtures.py

Writes (next to this file): fr24_rows.json, plan_legs.json, vamsys.json, search_index.json, meta.json.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[2]
sys.path.insert(0, str(WORK))

NUMBERS = ["CA1109", "CA754", "CA3155", "CA3156", "CA1005", "CA1006", "CA1303", "CA1805", "CA1806"]
CALLSIGNS = ["CCA1109", "CCA754", "CAO3155", "CAO3156", "CAO1005", "CAO1006", "CCA1303", "CCA1805", "CCA1806"]
ROW_KEYS = ("id", "number", "callsign", "airline_icao", "origin_icao", "origin_iata", "dest_icao", "dest_iata",
            "aircraft_code", "registration", "sched_dep", "sched_arr", "real_dep", "real_arr", "status_live",
            "status_text", "status_generic")


def _plain(v):
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    return v


def main() -> None:
    from vcca import caac
    from vcca.config import load_config
    from vcca.vamsys import load_vamsys

    cfg = load_config("2026-27-1")
    cache = cfg.cache_dir
    fh = json.loads((cache / "flight_histories.json").read_text(encoding="utf-8"))["flights"]
    rh = json.loads((cache / "reg_histories.json").read_text(encoding="utf-8"))["flights"]
    si = json.loads((cache / "search_index.json").read_text(encoding="utf-8"))
    rows = []
    for num in NUMBERS:
        for r in fh.get(num, {}).get("rows", []):
            rows.append({**{k: r.get(k) for k in ROW_KEYS}, "source": f"flight:{num}"})
    for fp in sorted((cache / "airports").glob("*.json")):
        for r in json.loads(fp.read_text(encoding="utf-8")).get("rows", []):
            if str(r.get("number") or "").upper() in NUMBERS or str(r.get("callsign") or "").upper() in CALLSIGNS:
                rows.append({**{k: r.get(k) for k in ROW_KEYS}, "source": f"board:{fp.stem.upper()}"})
    for reg, p in sorted(rh.items()):
        for r in p.get("rows", []):
            if str(r.get("callsign") or "").upper() in CALLSIGNS:
                rows.append({**{k: r.get(k) for k in ROW_KEYS}, "source": f"reg:{reg}"})
    (HERE / "fr24_rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")

    plan = caac.load_plan(cfg)
    legs = [{k: _plain(v) for k, v in dataclasses.asdict(l).items()} for l in plan.legs if l.callsign in CALLSIGNS]
    (HERE / "plan_legs.json").write_text(json.dumps(legs, ensure_ascii=False, indent=0), encoding="utf-8")

    vam = load_vamsys(cfg)
    routes = []
    aps = set()
    for r in vam.routes:
        if r.callsign in CALLSIGNS:
            d = {k: _plain(v) for k, v in dataclasses.asdict(r).items() if k not in ("fields", "raw")}
            routes.append(d)
            aps |= {r.orig, r.dest}
    for l in plan.legs:
        if l.callsign in CALLSIGNS:
            aps |= {cfg.vamsys_airport(l.orig), cfg.vamsys_airport(l.dest)}
    for r in rows:
        for k in ("origin_icao", "dest_icao"):
            if r.get(k):
                aps.add(cfg.vamsys_airport(r[k]))
    airports = {c: {"code": c, "icao": a.icao, "iata": a.iata, "name": a.name}
                for c, a in vam.airports.items() if c in aps}
    (HERE / "vamsys.json").write_text(json.dumps({"revision": vam.revision, "routes": routes, "airports": airports},
                                                 ensure_ascii=False, indent=0), encoding="utf-8")
    sub = {"by_flight": {k: v for k, v in (si.get("by_flight") or {}).items() if k in NUMBERS},
           "by_callsign": {k: v for k, v in (si.get("by_callsign") or {}).items() if k in CALLSIGNS}}
    (HERE / "search_index.json").write_text(json.dumps(sub, ensure_ascii=False, indent=0), encoding="utf-8")
    meta = {"numbers": NUMBERS, "callsigns": CALLSIGNS, "rows": len(rows), "legs": len(legs), "routes": len(routes),
            "source": "fr24_cache legacy JSON (flight_histories updated " + json.loads(
                (cache / "flight_histories.json").read_text(encoding="utf-8")).get("updated_at_utc", "") + ")"}
    (HERE / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(meta)


if __name__ == "__main__":
    main()
