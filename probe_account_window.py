"""Probe authenticated FR24 subscription capabilities and history window span (no PII logged)."""

import datetime
from FlightRadarAPI.errors import LoginError
from fr24_auth import create_logged_in_api, credentials_available


def fmt_ts(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main():
    if not credentials_available():
        print("CRED_MISSING")
        return

    try:
        fr = create_logged_in_api(timeout=25)
    except LoginError as e:
        print(f"LOGIN_FAILED: {e}")
        return

    print("LOGIN_OK:", fr.is_logged_in())
    ud = fr.get_login_data()
    print("userData keys:", sorted(ud.keys()))
    for k in ("subscription", "subscriptionKey", "typeAccount", "isPaid", "isActive", "roleId", "accountType"):
        if k in ud and k != "subscriptionKey":
            print(f"  {k}: {ud[k]}")
    if "subscriptions" in ud:
        print("  subscriptions:", ud["subscriptions"])

    # Probe airport page limits & timestamps when logged in
    for ap_code in ("ZBAA", "PANC"):
        p1 = fr.get_airport_details(ap_code, flight_limit=100, page=1)
        pn1 = fr.get_airport_details(ap_code, flight_limit=100, page=-1)
        s1 = ((p1.get("airport") or {}).get("pluginData") or {}).get("schedule") or {}
        sn1 = ((pn1.get("airport") or {}).get("pluginData") or {}).get("schedule") or {}
        pos_dep_tot = ((s1.get("departures") or {}).get("page") or {}).get("total")
        pos_arr_tot = ((s1.get("arrivals") or {}).get("page") or {}).get("total")
        neg_dep_tot = ((sn1.get("departures") or {}).get("page") or {}).get("total")
        neg_arr_tot = ((sn1.get("arrivals") or {}).get("page") or {}).get("total")
        print(f"\nAirport {ap_code}: pos(arr={pos_arr_tot}, dep={pos_dep_tot}), neg(arr={neg_arr_tot}, dep={neg_dep_tot})")

        # Fetch oldest negative page (up to -neg_dep_tot) and newest positive page
        for label, pg in (("oldest_neg_dep", -(int(neg_dep_tot or 1))), ("newest_pos_dep", int(pos_dep_tot or 1))):
            ap_edge = fr.get_airport_details(ap_code, flight_limit=100, page=pg)
            rows = ((((ap_edge.get("airport") or {}).get("pluginData") or {}).get("schedule") or {}).get("departures") or {}).get("data") or []
            ts_list = [((r.get("flight") or {}).get("time") or {}).get("scheduled", {}).get("departure") for r in rows]
            ts_list = [int(t) for t in ts_list if t]
            if ts_list:
                print(f"  {label} (page={pg}, rows={len(rows)}): {fmt_ts(min(ts_list))} .. {fmt_ts(max(ts_list))}")
            else:
                print(f"  {label} (page={pg}, rows={len(rows)}): empty timestamps")

    # Probe 1 live CCA flight for details/history depth
    fls = fr.get_flights(airline="CCA")
    print(f"\nLive CCA flights: {len(fls)}")
    if fls:
        fl = fls[0]
        det = fr.get_flight_details(fl)
        hist = (det.get("flightHistory") or {}).get("aircraft") or []
        hist_ts = [
            ((h.get("time") or {}).get("real") or {}).get("departure")
            or ((h.get("time") or {}).get("scheduled") or {}).get("departure")
            for h in hist
        ]
        hist_ts = [int(t) for t in hist_ts if t]
        print(f"  Sample aircraft history rows={len(hist)}, span={fmt_ts(min(hist_ts)) if hist_ts else None} .. {fmt_ts(max(hist_ts)) if hist_ts else None}")


if __name__ == "__main__":
    main()
