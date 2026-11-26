"""Fetch Gold-tier historical & future flight instances per flight number via FR24 flight/list.json.

Saves incrementally per flight number into fr24_cache/flight_histories.json so interrupted runs resume cleanly.
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from FlightRadarAPI.core import Core
from fr24_paths import CACHE_DIR
from fr24_auth import create_logged_in_api
import verify_with_fr24_final as audit_mod


OUT_PATH = os.path.join(CACHE_DIR, "flight_histories.json")
REG_OUT_PATH = os.path.join(CACHE_DIR, "reg_histories.json")


def compute_target_flight_numbers():
    """Return (tier1_priority_nums, tier2_remaining_nums) derived from plan + VAMSYS + current 3-day audit."""
    cat = json.load(open(os.path.join(CACHE_DIR, "plan_catalog.json"), encoding="utf-8"))
    to_num = lambda cs: ("CA" + str(cs)[3:]) if str(cs).startswith(("CCA", "CAO", "CCD", "BJN")) else str(cs)

    all_nums = set()
    for r in cat["seg"]:
        all_nums.add(to_num(r["源呼号"]))
    for r in cat["cur"]:
        if r["航司"] in ("CCA", "CAO", "CCD", "BJN"):
            n = str(r.get("现有班号") or to_num(r["现有呼号"])).strip().upper()
            if n:
                all_nums.add(n)

    tier1 = set()
    # Every flight in 拟停用 (590 groups) — to verify exact last-flown date vs 2026-10-25
    for r in cat["stop"]:
        n = str(r.get("现有班号") or to_num(r["现有呼号"])).strip().upper()
        if n:
            tier1.add(n)

    # Every flight NOT yet directly confirmed in final_audit.json or with anomalies/time deviations
    audit_path = os.path.join(CACHE_DIR, "final_audit.json")
    if os.path.exists(audit_path):
        aud = json.load(open(audit_path, encoding="utf-8"))
        for x in aud.get("seg_op_mismatch", []) + aud.get("seg_route_mismatch", []) + aud.get("seg_not_found", []):
            tier1.add(to_num(x[1]))
        for x in aud.get("expired_10_results", []) + aud.get("seg_time_diffs", []) + aud.get("adj_dep_devs", []) + aud.get("adj_arr_only_devs", []) + aud.get("adj_special_items", []):
            tier1.add(to_num(x["cs"]))
        for x in aud.get("add_issues", []):
            tier1.add(to_num(x[2]))
        for x in aud.get("unplanned_notable", []):
            if x.get("cs"):
                tier1.add(to_num(x["cs"]))

    # Also include every seg/add/adj/保留 group whose OD has zero observations in 111-airport/live cache
    live_data, _, ap_data = audit_mod.load_fr24()
    observed_ods = set()
    for fls in live_data.get("live", {}).values():
        for f in fls:
            n = (f.get("number") or "").upper()
            o = audit_mod.norm_ap(None, f.get("origin_iata"))
            d = audit_mod.norm_ap(None, f.get("destination_iata"))
            if n and o and d:
                observed_ods.add((n, o, d))
    for d_item in live_data.get("details", {}).values():
        n = (d_item.get("number_default") or "").upper()
        o = audit_mod.norm_ap(d_item.get("origin_icao"), d_item.get("origin_iata"))
        dest = audit_mod.norm_ap(d_item.get("dest_icao"), d_item.get("dest_iata"))
        if n and o and dest:
            observed_ods.add((n, o, dest))
        for h in d_item.get("history") or []:
            hn = (h.get("number") or "").upper()
            ho = audit_mod.norm_ap(h.get("origin_icao"), h.get("origin_iata"))
            hd = audit_mod.norm_ap(h.get("dest_icao"), h.get("dest_iata"))
            if hn and ho and hd:
                observed_ods.add((hn, ho, hd))
    for payload in ap_data.values():
        for r in payload.get("rows", []):
            n = (r.get("number") or "").upper()
            o = audit_mod.norm_ap(r.get("origin_icao"), r.get("origin_iata"))
            dest = audit_mod.norm_ap(r.get("dest_icao"), r.get("dest_iata"))
            if n and o and dest:
                observed_ods.add((n, o, dest))

    def has_od(num, orig, dest):
        o2 = audit_mod.ICAO_ALIASES.get(orig, orig)
        d2 = audit_mod.ICAO_ALIASES.get(dest, dest)
        return (num, orig, dest) in observed_ods or (num, o2, d2) in observed_ods

    for r in cat["seg"]:
        n = to_num(r["源呼号"])
        if not has_od(n, r["始发"], r["到达"]):
            tier1.add(n)
    for r in cat["add"]:
        n = str(r.get("候选班号") or to_num(r["计划呼号"])).upper()
        if not has_od(n, r["始发"], r["到达"]):
            tier1.add(n)
    for r in cat["adj"]:
        n = to_num(r["呼号"])
        if not has_od(n, r["始发"], r["到达"]):
            tier1.add(n)
    for r in cat["cur"]:
        if r.get("处理组") == "保留":
            n = str(r.get("现有班号") or to_num(r["现有呼号"])).upper()
            if not has_od(n, r["始发"], r["到达"]):
                tier1.add(n)

    # Ensure well-formatted flight numbers only
    tier1 = sorted(n for n in tier1 if n and len(n) >= 4)
    tier2 = sorted(n for n in all_nums if n not in set(tier1) and n and len(n) >= 4)
    return tier1, tier2


def parse_item(it):
    ident = it.get("identification") or {}
    al = (it.get("airline") or {}).get("code") or {}
    ac = it.get("aircraft") or {}
    ap = it.get("airport") or {}
    orig = (ap.get("origin") or {}).get("code") or {}
    dest = (ap.get("destination") or {}).get("code") or {}
    tm = it.get("time") or {}
    st = it.get("status") or {}
    return {
        "id": ident.get("id"),
        "number": (ident.get("number") or {}).get("default"),
        "callsign": ident.get("callsign"),
        "airline_icao": al.get("icao"),
        "origin_icao": orig.get("icao"),
        "origin_iata": orig.get("iata"),
        "dest_icao": dest.get("icao"),
        "dest_iata": dest.get("iata"),
        "aircraft_code": (ac.get("model") or {}).get("code"),
        "registration": ac.get("registration"),
        "sched_dep": (tm.get("scheduled") or {}).get("departure"),
        "sched_arr": (tm.get("scheduled") or {}).get("arrival"),
        "real_dep": (tm.get("real") or {}).get("departure"),
        "real_arr": (tm.get("real") or {}).get("arrival"),
        "status_live": st.get("live"),
        "status_text": st.get("text"),
        "status_generic": ((st.get("generic") or {}).get("status") or {}).get("text"),
    }


def fetch_one_flight(client, subkey, flight_num, fetch_by="flight", page=1):
    url = f"{Core.api_flightradar_base_url}/flight/list.json"
    for attempt in range(4):
        time.sleep(0.45)
        try:
            resp = client.request_standalone(
                url,
                params={"query": flight_num, "fetchBy": fetch_by, "page": page, "limit": 100, "token": subkey},
                headers=Core.json_headers,
                allowed_error_codes=[400, 401, 403],
                timeout=25,
            )
            sc = resp.get_status_code()
            if sc != 200:
                return flight_num, [], f"http_{sc}", False
            data = resp.get_json_content() or {}
            res = (data.get("result") or {}).get("response") or {}
            raw_items = res.get("data") or []
            parsed = [parse_item(it) for it in raw_items]
            more = bool((res.get("page") or {}).get("more"))
            return flight_num, parsed, None, more
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                time.sleep(15.0 * (attempt + 1))
                continue
            return flight_num, [], repr(e), False
    return flight_num, [], "max_retries", False


def save_cache(store, errors, out_path=None):
    out_path = out_path or OUT_PATH
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_cached": len(store),
            "errors_count": len(errors),
            "flights": store,
            "errors": errors,
        }, f, ensure_ascii=False)
    os.replace(tmp, out_path)


WINTER_START_TS = 1792857600  # 2026-10-25 00:00 BJT


def fetch_reg_history(client, subkey, reg, stop_ts=WINTER_START_TS, max_pages=6):
    """All flights flown by one aircraft registration, paging back until the season start. Unlike flight-number queries this also
    returns continuation legs FR24 publishes with a callsign but no flight number (e.g. CAO3155 ESSA->EBLG)."""
    rows, more = [], False
    for page in range(1, max_pages + 1):
        _, items, err, more = fetch_one_flight(client, subkey, reg, fetch_by="reg", page=page)
        if err:
            return reg, rows, err, more
        rows.extend(items)
        oldest = min((int(r.get("sched_dep") or r.get("real_dep") or 0) for r in items if r.get("sched_dep") or r.get("real_dep")), default=0)
        if not more or not items or (oldest and oldest < stop_ts):
            break
    return reg, rows, None, more


def cao_registrations():
    """Aircraft registrations seen operating CAO (callsign CAOxxxx or airline CAO) anywhere in the FR24 caches."""
    regs = set()

    def take(r, cs_key="callsign"):
        cs = (r.get(cs_key) or "").upper()
        if (cs.startswith("CAO") or (r.get("airline_icao") or "").upper() == "CAO") and r.get("registration"):
            regs.add(r["registration"].upper())

    for payload in json.load(open(OUT_PATH, encoding="utf-8")).get("flights", {}).values():
        for r in payload.get("rows", []):
            take(r)
    ap_dir = os.path.join(CACHE_DIR, "airports")
    for fn in os.listdir(ap_dir):
        if fn.endswith(".json"):
            for r in json.load(open(os.path.join(ap_dir, fn), encoding="utf-8")).get("rows", []):
                take(r)
    return sorted(regs)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    out_path = OUT_PATH
    worker = fetch_one_flight
    if mode in ("reg", "reg-plan"):
        # Registration histories: the only FR24 query that returns callsign-only continuation legs.
        out_path, worker = REG_OUT_PATH, fetch_reg_history
        targets = sorted(set(cao_registrations()) | {c.upper() for c in sys.argv[2:]})
        store = {}
        if os.path.exists(out_path):
            store = json.load(open(out_path, "r", encoding="utf-8")).get("flights", {})
        todo = [q for q in targets if q not in store]
        print(f"Mode={mode}: registration targets={len(targets)}, already_cached={len(store)}, todo={len(todo)}", flush=True)
        print("  " + " ".join(todo), flush=True)
        if mode == "reg-plan" or not todo:
            return
    elif mode in ("search", "search-plan"):
        # Flights FR24 search knows for CCA/CAO/CCD/BJN but absent from plan + VAMSYS (e.g. CA1861) were never fetched,
        # so the rebuild's "FR24-only unplanned" detection could only see them on 3-day airport boards.
        search = json.load(open(os.path.join(CACHE_DIR, "search_index.json"), encoding="utf-8"))
        targets = sorted(
            k.upper() for k, v in search.get("by_flight", {}).items()
            if ((v.get("detail") or {}).get("operator") or "").upper() in ("CCA", "CAO", "CCD", "BJN") and len(k) >= 4
        )
        store = {}
        if os.path.exists(OUT_PATH):
            store = json.load(open(OUT_PATH, "r", encoding="utf-8")).get("flights", {})
        todo = [q for q in targets if q not in store]
        print(f"Mode={mode}: search targets={len(targets)}, already_cached={len(store)}, todo={len(todo)}", flush=True)
        if mode == "search-plan" or not todo:
            return
    elif mode == "file":
        list_path = sys.argv[2]
        targets = json.load(open(list_path, "r", encoding="utf-8"))
        store = {}
        if os.path.exists(OUT_PATH):
            store = json.load(open(OUT_PATH, "r", encoding="utf-8")).get("flights", {})
        todo = [q for q in targets if q not in store]
        print(f"Mode=file({list_path}): targets={len(targets)}, already_cached={len(store)}, todo={len(todo)}", flush=True)
        if not todo:
            print("Nothing to fetch.")
            return
    else:
        t1, t2 = compute_target_flight_numbers()
        if mode == "plan":
            print(f"Target plan: tier1={len(t1)}, tier2={len(t2)}, total={len(t1)+len(t2)}")
            return

        targets = t1 if mode == "tier1" else (t1 + t2 if mode == "all" else t2)
        store = {}
        if os.path.exists(OUT_PATH):
            store = json.load(open(OUT_PATH, "r", encoding="utf-8")).get("flights", {})
        todo = [q for q in targets if q not in store]
        print(f"Mode={mode}: targets={len(targets)}, already_cached={len(store)}, todo={len(todo)}", flush=True)
        if not todo:
            print("Nothing to fetch.")
            return

    fr, meta = create_logged_in_api(timeout=25, max_workers=2)
    client = fr._FlightRadar24API__client
    subkey = meta.get("subscriptionKey", "")
    print(f"Authenticated Gold session ready (key_present={bool(subkey)}). Starting fetch...", flush=True)

    errors = []
    t0 = time.time()
    done = 0
    consec_err = 0
    aborted = False
    batch_size = 40
    with ThreadPoolExecutor(max_workers=2) as ex:
        for i in range(0, len(todo), batch_size):
            batch = todo[i:i + batch_size]
            futs = {ex.submit(worker, client, subkey, fn): fn for fn in batch}
            for fut in as_completed(futs):
                done += 1
                q, rows, err, more = fut.result()
                if err:
                    errors.append((q, err))
                    consec_err += 1
                else:
                    store[q] = {"rows": rows, "more": more}
                    consec_err = 0
                if consec_err >= 8:
                    aborted = True
            save_cache(store, errors, out_path)
            print(f"  [{done}/{len(todo)}] cached={len(store)} err={len(errors)} elapsed={time.time()-t0:.1f}s", flush=True)
            if aborted:
                print("Aborted early due to consecutive errors (cache saved).", flush=True)
                break

    save_cache(store, errors, out_path)
    print(f"Done! total_cached={len(store)}, err={len(errors)}, elapsed={time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
