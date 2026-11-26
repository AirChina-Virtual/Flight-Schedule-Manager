import os, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from FlightRadarAPI import FlightRadar24API
from FlightRadarAPI.request import RetryPolicy
from fr24_paths import CACHE_DIR

os.makedirs(CACHE_DIR, exist_ok=True)
out_path = os.path.join(CACHE_DIR, "search_index.json")


def main():
    with open(os.path.join(CACHE_DIR, "plan_catalog.json"), "r", encoding="utf-8") as f:
        cat = json.load(f)

    prefix_to_full = defaultdict(set)
    for r in cat["seg"]:
        cs = str(r["源呼号"]).strip()
        if cs.startswith(("CCA", "CAO", "CCD", "BJN")):
            fn = "CA" + cs[3:]
            prefix_to_full[fn[:5]].add(fn)
            if cs.startswith("BJN"):
                prefix_to_full[cs[:5]].add(cs)
    for r in cat["add"]:
        fn = str(r.get("候选班号") or "").strip()
        if fn:
            prefix_to_full[fn[:5]].add(fn)
    for r in cat["cur"]:
        if r["航司"] in ("CCA", "CAO", "CCD", "BJN"):
            fn = str(r.get("现有班号") or "").strip()
            cs = str(r.get("现有呼号") or "").strip()
            if fn:
                prefix_to_full[fn[:5]].add(fn)
            if cs.startswith("BJN"):
                prefix_to_full[cs[:5]].add(cs)

    queries = sorted(prefix_to_full.keys())
    print(f"Total unique 5-char search prefixes: {len(queries)}", flush=True)

    fr = FlightRadar24API(timeout=20, retry=RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=15.0))

    results = {}
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            results = json.load(f).get("by_query", {})
        print(f"Resuming with {len(results)} cached queries", flush=True)

    def safe_search(q):
        for attempt in range(4):
            try:
                return fr.search(q, limit=50)
            except Exception as e:
                if "429" in str(e) and attempt < 3:
                    time.sleep(6.0 * (attempt + 1))
                    continue
                raise

    todo = [q for q in queries if q not in results]
    errors = []

    def run_one(q):
        time.sleep(0.22)
        res = safe_search(q)
        sched = res.get("schedule") or []
        live = res.get("live") or []
        return q, {"schedule": sched, "live": live}

    def save_all():
        by_flight = {}
        by_callsign = {}
        for q, d in results.items():
            for item in d.get("schedule", []):
                det = item.get("detail") or {}
                op = det.get("operator")
                cs = det.get("callsign")
                fl = det.get("flight") or item.get("id")
                if op in ("CCA", "CAO", "CCD", "BJN") or str(fl).startswith(("CA", "BJN", "CAO", "CCD")):
                    if fl:
                        by_flight[fl] = item
                    if cs:
                        by_callsign[cs] = item
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total_queries": len(queries),
                "errors": errors,
                "by_flight": by_flight,
                "by_callsign": by_callsign,
                "by_query": results,
            }, f, ensure_ascii=False)
        return len(by_flight), len(by_callsign)

    t0 = time.time()
    capped = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(run_one, q): q for q in todo}
        done = 0
        for fut in as_completed(futs):
            q = futs[fut]
            done += 1
            try:
                _, data = fut.result()
                results[q] = data
                if len(data["schedule"]) >= 25:
                    capped.append(q)
            except Exception as e:
                errors.append((q, repr(e)))
            if done % 60 == 0 or done == len(todo):
                nf, nc = save_all()
                print(f"  [{done}/{len(todo)}] elapsed={time.time()-t0:.1f}s by_flight={nf} errors={len(errors)} capped={len(capped)}", flush=True)

    if capped:
        extra = sorted({full for p in capped for full in prefix_to_full[p] if full not in results})
        print(f"Fetching {len(extra)} exact flight strings for {len(capped)} capped prefixes...", flush=True)
        for idx, q in enumerate(extra, 1):
            time.sleep(0.22)
            try:
                _, data = run_one(q)
                results[q] = data
            except Exception as e:
                errors.append((q, repr(e)))

    nf, nc = save_all()
    print(f"Done! by_flight={nf}, by_callsign={nc}, errors={len(errors)}", flush=True)


if __name__ == "__main__":
    main()
