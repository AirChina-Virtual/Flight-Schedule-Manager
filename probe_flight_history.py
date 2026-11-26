"""Probe Gold feature limits, sample flight/list.json histories, and plan full fetch volume."""

import json, datetime
from collections import defaultdict
from FlightRadarAPI.core import Core
from fr24_auth import create_logged_in_api
from fr24_paths import CACHE_DIR


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    fr, meta = create_logged_in_api(timeout=25)
    client = fr._FlightRadar24API__client
    subkey = meta.get("subscriptionKey", "")
    feats = meta.get("features") or {}
    print("Logged in:", fr.is_logged_in(), "| cached_at:", meta.get("saved_at_utc"))
    for k in sorted(feats.keys()):
        if "history" in k or "airport" in k:
            print(f"  feature {k}: {feats[k]}")

    # Check if airport.json changes with token=subKey
    ap_p1 = client.request(
        Core.api_airport_data_url,
        params={"format": "json", "code": "ZBAA", "limit": 100, "page": -1, "token": subkey},
        headers=Core.json_headers, timeout=25,
    ).get_json_content()
    sn = ((ap_p1.get("airport") or {}).get("pluginData") or {}).get("schedule") or {}
    print("ZBAA with subKey neg total pages:", ((sn.get("arrivals") or {}).get("page") or {}).get("total"), ((sn.get("departures") or {}).get("page") or {}).get("total"))

    # Check flight/list.json with subKey on key sample flight numbers
    url = f"{Core.api_flightradar_base_url}/flight/list.json"
    t_start = int(datetime.datetime(2026, 10, 25, tzinfo=datetime.timezone.utc).timestamp())
    for q in ("CA1323", "CA8395", "CA8627", "CA1049", "CA750", "CA416", "CA620", "CA1963", "CA442", "CA754"):
        resp = client.request(url, params={"query": q, "fetchBy": "flight", "page": 1, "limit": 100, "token": subkey}, headers=Core.json_headers, timeout=25)
        res = (resp.get_json_content() or {}).get("result", {}).get("response", {})
        items = res.get("data") or []
        summary = defaultdict(int)
        all_ts = []
        winter_cnt = 0
        for it in items:
            cs = (it.get("identification") or {}).get("callsign") or ""
            al = ((it.get("airline") or {}).get("code") or {}).get("icao") or ""
            o = ((it.get("airport") or {}).get("origin") or {}).get("code", {}).get("icao") or ""
            d = ((it.get("airport") or {}).get("destination") or {}).get("code", {}).get("icao") or ""
            tm = it.get("time") or {}
            t = (tm.get("real") or {}).get("departure") or (tm.get("scheduled") or {}).get("departure")
            summary[(al, cs[:3], o, d)] += 1
            if t:
                all_ts.append(int(t))
                if int(t) >= t_start:
                    winter_cnt += 1
        span = f"{fmt_ts(min(all_ts))}..{fmt_ts(max(all_ts))}" if all_ts else "none"
        print(f"  {q:<7}: total={len(items):<3} winter(>=10/25)={winter_cnt:<2} span={span} | top={dict(list(summary.items())[:3])}")

    cat = json.load(open(f"{CACHE_DIR}/plan_catalog.json", encoding="utf-8"))
    nums = set()
    for r in cat["seg"]:
        cs = str(r["源呼号"]).strip()
        if cs.startswith(("CCA", "CAO", "CCD", "BJN")):
            nums.add("CA" + cs[3:])
    for r in cat["cur"]:
        if r["航司"] in ("CCA", "CAO", "CCD", "BJN") and r.get("现有班号"):
            nums.add(str(r["现有班号"]).strip().upper())
    print("\nTotal unique flight numbers across plan + VAMSYS:", len(nums))


if __name__ == "__main__":
    main()
