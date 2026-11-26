import os, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from FlightRadarAPI import FlightRadar24API
from FlightRadarAPI.request import RetryPolicy
from fr24_paths import CACHE_DIR

os.makedirs(CACHE_DIR, exist_ok=True)
out_path = os.path.join(CACHE_DIR, "live_summary.json")


def main():
    fr = FlightRadar24API(timeout=15, max_workers=4, retry=RetryPolicy(max_attempts=2, base_delay=1.5, max_delay=5.0))

    all_live = {}
    all_details = {}
    errors = []

    if os.path.exists(out_path):
        prev = json.load(open(out_path, "r", encoding="utf-8"))
        all_details = prev.get("details", {})

    t0 = time.time()
    all_flights_objs = []
    for op in ["CCA", "CAO", "CCD", "BJN"]:
        flights = fr.get_flights(airline=op)
        print(f"{op}: {len(flights)} live flights ({time.time()-t0:.1f}s)", flush=True)
        all_live[op] = []
        for f in flights:
            all_live[op].append({
                "id": f.id,
                "callsign": f.callsign,
                "number": f.number,
                "airline_icao": f.airline_icao,
                "airline_iata": f.airline_iata,
                "aircraft_code": f.aircraft_code,
                "registration": f.registration,
                "origin_iata": f.origin_airport_iata,
                "destination_iata": f.destination_airport_iata,
                "on_ground": f.on_ground,
                "altitude": f.altitude,
                "ground_speed": f.ground_speed,
                "time": f.time,
            })
            all_flights_objs.append((op, f))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "live": all_live, "details": all_details, "errors": errors}, f, ensure_ascii=False)

    todo = [(op, fl) for op, fl in all_flights_objs if fl.id not in all_details]
    print(f"Fetching details for {len(todo)} flights...", flush=True)

    def fetch_det(item):
        op, fl = item
        time.sleep(0.05)
        return op, fl.id, fr.get_flight_details(fl)

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_det, item): item for item in todo}
        for fut in as_completed(futs):
            op, fl = futs[fut]
            done += 1
            try:
                op, fid, det = fut.result()
                orig = (det.get("airport") or {}).get("origin") or {}
                dest = (det.get("airport") or {}).get("destination") or {}
                ident = det.get("identification") or {}
                ac = det.get("aircraft") or {}
                al = det.get("airline") or {}
                tm = det.get("time") or {}
                st = det.get("status") or {}
                hist_raw = (det.get("flightHistory") or {}).get("aircraft") or []
                hist_clean = []
                for h in hist_raw:
                    h_orig = (h.get("airport") or {}).get("origin") or {}
                    h_dest = (h.get("airport") or {}).get("destination") or {}
                    hist_clean.append({
                        "id": (h.get("identification") or {}).get("id"),
                        "number": ((h.get("identification") or {}).get("number") or {}).get("default"),
                        "origin_icao": (h_orig.get("code") or {}).get("icao"),
                        "origin_iata": (h_orig.get("code") or {}).get("iata"),
                        "dest_icao": (h_dest.get("code") or {}).get("icao"),
                        "dest_iata": (h_dest.get("code") or {}).get("iata"),
                        "time": h.get("time"),
                    })
                all_details[fid] = {
                    "id": fid,
                    "operator_query": op,
                    "callsign": ident.get("callsign"),
                    "number_default": (ident.get("number") or {}).get("default"),
                    "number_alt": (ident.get("number") or {}).get("alternative"),
                    "airline_icao": (al.get("code") or {}).get("icao"),
                    "airline_iata": (al.get("code") or {}).get("iata"),
                    "aircraft_code": (ac.get("model") or {}).get("code"),
                    "aircraft_text": (ac.get("model") or {}).get("text"),
                    "registration": ac.get("registration"),
                    "origin_icao": (orig.get("code") or {}).get("icao"),
                    "origin_iata": (orig.get("code") or {}).get("iata"),
                    "dest_icao": (dest.get("code") or {}).get("icao"),
                    "dest_iata": (dest.get("code") or {}).get("iata"),
                    "time": tm,
                    "status_text": st.get("text"),
                    "history": hist_clean,
                }
            except Exception as e:
                errors.append((op, fl.id, repr(e)))
            if done % 25 == 0 or done == len(todo):
                print(f"  [{done}/{len(todo)}] ok={len(all_details)} err={len(errors)} ({time.time()-t0:.1f}s)", flush=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump({"fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "live": all_live, "details": all_details, "errors": errors}, f, ensure_ascii=False)

    total_hist = sum(len(v["history"]) for v in all_details.values())
    print(f"Done in {time.time()-t0:.1f}s! details={len(all_details)}, hist={total_hist}, err={len(errors)}", flush=True)
    if errors:
        print("Sample errors:", errors[:5], flush=True)


if __name__ == "__main__":
    main()
