import os, json, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from FlightRadarAPI import FlightRadar24API
from FlightRadarAPI.request import RetryPolicy
from fr24_paths import AP_CACHE_DIR

cache_dir = AP_CACHE_DIR
os.makedirs(cache_dir, exist_ok=True)

COVER41_PLUS_ZBAL = [
    'ZBAA', 'ZUTF', 'ZBAD', 'ZUCK', 'ZUUU', 'ZSPD', 'ZSHC', 'ZBTJ', 'ZHHH', 'ZBHH',
    'ZGGG', 'ZSWZ', 'ZWWW', 'ZYTL', 'ZGSZ', 'LEMD', 'ZYHB', 'ZYTX', 'ZLLL', 'PANC',
    'ESSA', 'ZLXY', 'ZUDA', 'KLAX', 'ZPPP', 'ZULS', 'ZBYC', 'ZSSS', 'RKSI', 'ZWAK',
    'ZLIC', 'ZLXN', 'ZHCC', 'ZWYN', 'ZBLA', 'ZBES', 'ZGHA', 'EKCH', 'LROP', 'OPIS',
    'MUHA', 'ZBAL',
]

BATCH2_AIRPORTS = [
    'EBLG', 'EDDF', 'EGPK', 'EHAM', 'KJFK', 'KMIA', 'KORD', 'KPAE', 'LFPG', 'LHBP',
    'LIMC', 'LSZH', 'LTFM', 'MMSM', 'OMDW', 'RCTP', 'RJBB', 'SKBO', 'SVMI', 'UUEE',
    'VHHH', 'VIDP', 'VVNB', 'ZBTL', 'ZBUL', 'ZBXH', 'ZGBH', 'ZGHY', 'ZGKL', 'ZGNN',
    'ZGSD', 'ZGZH', 'ZHSY', 'ZJHK', 'ZMCK', 'ZMMC', 'ZSAM', 'ZSCN', 'ZSGZ', 'ZSJD',
    'ZSJN', 'ZSJU', 'ZSNB', 'ZSNJ', 'ZSQD', 'ZSTX', 'ZSYA', 'ZUBZ', 'ZUGY', 'ZUMY',
    'ZUYB', 'ZYSD',
]

TARGET_ICAOS = {"CCA", "CAO", "CCD", "BJN"}


def is_target_flight(fl):
    al_code = (fl.get("airline") or {}).get("code") or {}
    al_icao = (al_code.get("icao") or "").upper()
    al_iata = (al_code.get("iata") or "").upper()
    ident = fl.get("identification") or {}
    cs = (ident.get("callsign") or "").upper()
    num = ((ident.get("number") or {}).get("default") or "").upper()
    alt = ((ident.get("number") or {}).get("alternative") or "").upper()
    if al_icao in TARGET_ICAOS:
        return True
    if any(x.startswith(("CCA", "CAO", "CCD", "BJN")) for x in (cs, num, alt) if x):
        return True
    if al_iata == "CA" or num.startswith("CA"):
        return True
    return False


def extract_row(airport_icao, direction, page, item):
    fl = item.get("flight") or {}
    if not is_target_flight(fl):
        return None
    ident = fl.get("identification") or {}
    al = fl.get("airline") or {}
    al_code = al.get("code") or {}
    ac = fl.get("aircraft") or {}
    ap = fl.get("airport") or {}
    orig = ap.get("origin") or {}
    dest = ap.get("destination") or {}
    orig_code = orig.get("code") or {}
    dest_code = dest.get("code") or {}
    tm = fl.get("time") or {}
    st = fl.get("status") or {}
    return {
        "queried_airport": airport_icao,
        "direction": direction,
        "page": page,
        "id": ident.get("id"),
        "row": ident.get("row"),
        "number": (ident.get("number") or {}).get("default"),
        "number_alt": (ident.get("number") or {}).get("alternative"),
        "callsign": ident.get("callsign"),
        "codeshare": ident.get("codeshare"),
        "airline_icao": al_code.get("icao"),
        "airline_iata": al_code.get("iata"),
        "airline_name": al.get("name"),
        "origin_icao": orig_code.get("icao") or (airport_icao if direction == "departure" else None),
        "origin_iata": orig_code.get("iata"),
        "dest_icao": dest_code.get("icao") or (airport_icao if direction == "arrival" else None),
        "dest_iata": dest_code.get("iata"),
        "aircraft_code": (ac.get("model") or {}).get("code"),
        "aircraft_text": (ac.get("model") or {}).get("text"),
        "registration": ac.get("registration"),
        "sched_dep": (tm.get("scheduled") or {}).get("departure"),
        "sched_arr": (tm.get("scheduled") or {}).get("arrival"),
        "real_dep": (tm.get("real") or {}).get("departure"),
        "real_arr": (tm.get("real") or {}).get("arrival"),
        "est_dep": (tm.get("estimated") or {}).get("departure"),
        "est_arr": (tm.get("estimated") or {}).get("arrival"),
        "status_live": st.get("live"),
        "status_text": st.get("text"),
        "status_generic": ((st.get("generic") or {}).get("status") or {}).get("text"),
    }


def safe_get_ap(fr, icao, page):
    for attempt in range(4):
        try:
            return fr.get_airport_details(icao, flight_limit=100, page=page)
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                time.sleep(8.0 * (attempt + 1))
                continue
            raise


def fetch_one_airport(fr, icao):
    out_path = os.path.join(cache_dir, f"{icao}.json")
    if os.path.exists(out_path):
        prev = json.load(open(out_path, "r", encoding="utf-8"))
        if not prev.get("errors"):
            return icao, "cached", len(prev.get("rows", []))
        rows = list(prev.get("rows", []))
        pages_fetched = list(prev.get("pages_fetched", []))
        total_pos = prev.get("total_pos", 1)
        total_neg = prev.get("total_neg", 1)
        retry_pages = [e[0] for e in prev.get("errors", [])]
        new_errors = []
        for p in retry_pages:
            time.sleep(0.35)
            try:
                ap = safe_get_ap(fr, icao, p)
                if p not in pages_fetched:
                    pages_fetched.append(p)
                sched = (ap.get("airport") or {}).get("pluginData", {}).get("schedule") or {}
                for kind, dir_name in (("arrivals", "arrival"), ("departures", "departure")):
                    block = sched.get(kind) or {}
                    pg = block.get("page") or {}
                    tot = int(pg.get("total") or 1)
                    if p == 1:
                        total_pos = max(total_pos, tot)
                    elif p == -1:
                        total_neg = max(total_neg, tot)
                    for item in block.get("data") or []:
                        r = extract_row(icao, dir_name, p, item)
                        if r:
                            rows.append(r)
            except Exception as e:
                new_errors.append((p, repr(e)))
        payload = {
            "airport": icao,
            "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_pos": total_pos,
            "total_neg": total_neg,
            "pages_fetched": pages_fetched,
            "errors": new_errors,
            "rows": rows,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return icao, f"retried={len(retry_pages)},err={len(new_errors)}", len(rows)

    rows = []
    errors = []
    pages_fetched = []
    total_pos = 1
    total_neg = 1

    for p in (1, -1):
        time.sleep(0.35)
        try:
            ap = safe_get_ap(fr, icao, p)
            pages_fetched.append(p)
            sched = (ap.get("airport") or {}).get("pluginData", {}).get("schedule") or {}
            for kind, dir_name in (("arrivals", "arrival"), ("departures", "departure")):
                block = sched.get(kind) or {}
                pg = block.get("page") or {}
                tot = int(pg.get("total") or 1)
                if p == 1:
                    total_pos = max(total_pos, tot)
                else:
                    total_neg = max(total_neg, tot)
                for item in block.get("data") or []:
                    r = extract_row(icao, dir_name, p, item)
                    if r:
                        rows.append(r)
        except Exception as e:
            errors.append((p, repr(e)))

    rem_pages = list(range(2, min(total_pos, 12) + 1)) + list(range(-2, -min(total_neg, 12) - 1, -1))
    for p in rem_pages:
        time.sleep(0.35)
        try:
            ap = safe_get_ap(fr, icao, p)
            pages_fetched.append(p)
            sched = (ap.get("airport") or {}).get("pluginData", {}).get("schedule") or {}
            for kind, dir_name in (("arrivals", "arrival"), ("departures", "departure")):
                block = sched.get(kind) or {}
                for item in block.get("data") or []:
                    r = extract_row(icao, dir_name, p, item)
                    if r:
                        rows.append(r)
        except Exception as e:
            errors.append((p, repr(e)))

    payload = {
        "airport": icao,
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_pos": total_pos,
        "total_neg": total_neg,
        "pages_fetched": pages_fetched,
        "errors": errors,
        "rows": rows,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return icao, f"pos={total_pos},neg={total_neg},err={len(errors)}", len(rows)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "batch1"
    if arg == "batch1":
        airports = COVER41_PLUS_ZBAL
    elif arg == "batch2":
        airports = BATCH2_AIRPORTS
    elif arg == "all":
        airports = COVER41_PLUS_ZBAL + BATCH2_AIRPORTS
    else:
        airports = sys.argv[1:]

    fr = FlightRadar24API(timeout=20, retry=RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=15.0))
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(fetch_one_airport, fr, icao): icao for icao in airports}
        for fut in as_completed(futs):
            icao = futs[fut]
            done += 1
            try:
                _, info, n = fut.result()
                print(f"[{done}/{len(airports)}] {icao}: {info}, matched_rows={n} ({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:
                print(f"[{done}/{len(airports)}] {icao}: FATAL {e!r}", flush=True)


if __name__ == "__main__":
    main()
