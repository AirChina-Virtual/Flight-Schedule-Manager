import os, re, json, datetime
from collections import Counter, defaultdict
import openpyxl

from fr24_paths import CACHE_DIR as cache_dir, SEASON_DIR as raw_plan_dir, OUT_XLSX as out_xlsx
cat = json.load(open(f"{cache_dir}/plan_catalog.json", encoding="utf-8"))
iata_to_icao = dict(cat["iata_to_icao"])

ICAO_ALIASES = {
    "ZSGS": "ZSJA",  # 井冈山
    "ZPMS": "ZPLX",  # 德宏芒市
}

CARGO_AC = {"77F", "B77F", "B77L", "77L", "74Y", "74F", "B74F", "33F"}
TARGET_OPS = ("CCA", "CAO", "CCD", "BJN")
WINTER_START_TS = int(datetime.datetime(2026, 10, 24, 16, 0, tzinfo=datetime.timezone.utc).timestamp())  # 2026-10-25 00:00 BJT
NOV_START_TS = int(datetime.datetime(2026, 11, 1, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
ACTIVE_SCHED_CUTOFF_TS = int(datetime.datetime(2026, 11, 15, 0, 0, tzinfo=datetime.timezone.utc).timestamp())


def load_fr24():
    live = json.load(open(f"{cache_dir}/live_summary.json", encoding="utf-8")) if os.path.exists(f"{cache_dir}/live_summary.json") else {"live": {}, "details": {}}
    search = json.load(open(f"{cache_dir}/search_index.json", encoding="utf-8")) if os.path.exists(f"{cache_dir}/search_index.json") else {"by_flight": {}, "by_callsign": {}}
    fh = json.load(open(f"{cache_dir}/flight_histories.json", encoding="utf-8")).get("flights", {}) if os.path.exists(f"{cache_dir}/flight_histories.json") else {}
    airports = {}
    ap_dir = f"{cache_dir}/airports"
    if os.path.isdir(ap_dir):
        for fn in os.listdir(ap_dir):
            if fn.endswith(".json"):
                airports[fn[:-5]] = json.load(open(os.path.join(ap_dir, fn), encoding="utf-8"))
    return live, search, airports, fh


def norm_ap(icao, iata):
    if icao:
        return str(icao).upper().strip()
    if iata:
        return iata_to_icao.get(str(iata).upper().strip(), str(iata).upper().strip())
    return None


def fmt_hm(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%H:%M")


def fmt_dt(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_date(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def hm_to_min(hm):
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def hhmm_diff_min(hm1, hm2):
    """Signed circular difference hm1 - hm2 in [-720, 719] minutes."""
    return ((hm_to_min(hm1) - hm_to_min(hm2) + 720) % 1440) - 720


def parse_hhmm_slots(text):
    """Extract all (dep_hhmm, arr_hhmm) pairs from VAMSYS/plan schedule text."""
    if not text:
        return []
    slots = []
    for line in str(text).splitlines():
        m = re.search(r"(\d{2}:\d{2})\s*[–\-~—]\s*(\d{2}:\d{2})", line)
        if m:
            slots.append((m.group(1), m.group(2)))
    return slots


def row_is_actual(r):
    return bool(
        r.get("real_dep")
        or r.get("real_arr")
        or r.get("status_live")
        or str(r.get("status_generic") or "").lower() in ("landed", "departed")
    )


def search_operator(by_flight_search, num):
    if not num:
        return ""
    return (((by_flight_search.get(num) or {}).get("detail") or {}).get("operator") or "").upper()


def is_cao_placeholder_row(cs, num, ac_code, is_actual, cao_flight_nums, by_flight_search):
    """True when FR24 stamps a pre-departure/estimated CAO freighter with CCAxxxx or empty callsign."""
    if is_actual:
        return False
    is_cargo_num = (ac_code in CARGO_AC) or (num in cao_flight_nums) or (search_operator(by_flight_search, num) == "CAO")
    return is_cargo_num and (not cs or cs.startswith("CCA"))


def classify_fr24_row(r, default_num, fn_auth, cao_flight_nums, by_flight_search):
    """Normalize (op, cs, num, op_source, is_actual) for airport board or flight_histories rows."""
    raw_al = (r.get("airline_icao") or "").upper()
    cs = (r.get("callsign") or "").upper()
    num = (r.get("number") or default_num or "").upper()
    alt = (r.get("number_alt") or "").upper()
    ac_code = (r.get("aircraft_code") or "").upper()

    if raw_al and raw_al not in TARGET_OPS:
        return None
    if cs and len(cs) >= 3 and cs[:3].isalpha() and not cs.startswith(TARGET_OPS):
        return None

    is_actual = row_is_actual(r)
    s_op = search_operator(by_flight_search, num)
    is_cao_ph = is_cao_placeholder_row(cs, num, ac_code, is_actual, cao_flight_nums, by_flight_search)
    is_actual_cao_cca = bool(is_actual and cs.startswith("CCA") and (ac_code in CARGO_AC or num in cao_flight_nums or s_op == "CAO"))

    op_source = "airport_callsign"
    if cs.startswith(TARGET_OPS) and not is_cao_ph:
        op = cs[:3]
    elif alt.startswith(TARGET_OPS) and not (not is_actual and alt.startswith("CCA") and (ac_code in CARGO_AC or num in cao_flight_nums or s_op == "CAO")):
        op = alt[:3]
        cs = alt
    elif is_cao_ph or ac_code in CARGO_AC or num in cao_flight_nums or s_op == "CAO":
        op = "CAO"
        op_source = "cargo_fallback"
        cs = ("CAO" + num[2:]) if num.startswith("CA") else (num or cs)
    elif num in fn_auth:
        op, cs = fn_auth[num]
        op_source = "search_resolved"
    elif raw_al in TARGET_OPS:
        op = raw_al
        op_source = "airport_iata_fallback"
        cs = (op + num[2:]) if num.startswith("CA") else (num or "")
    else:
        return None

    return {
        "op": op,
        "cs": cs,
        "num": num,
        "op_source": op_source,
        "is_actual": is_actual,
        "is_actual_cao_cca": is_actual_cao_cca,
        "ac_code": ac_code,
        "s_op": s_op,
    }


def main():
    live_data, search_data, ap_data, fh_data = load_fr24()
    by_flight_search = search_data.get("by_flight", {})
    by_cs_search = search_data.get("by_callsign", {})

    # Known CAO flight numbers from plan + VAMSYS
    cao_flight_nums = set()
    for r in cat["seg"]:
        if r["航司"] == "CAO":
            cao_flight_nums.add("CA" + r["源呼号"][3:])
    for r in cat["cur"]:
        if r["航司"] == "CAO" and r.get("现有班号"):
            cao_flight_nums.add(str(r["现有班号"]).strip().upper())

    # Authoritative flight_number -> (operator, callsign)
    # Only use Winter (>= 2026-10-25 BJT) actuals before scheduled/search fallback so historical ops don't pollute current carriers
    fn_auth = {}
    for op, fls in live_data.get("live", {}).items():
        for f in fls:
            cs = (f.get("callsign") or "").upper()
            num = (f.get("number") or "").upper()
            al_op = (f.get("airline_icao") or op).upper()
            if num and cs and al_op in TARGET_OPS:
                fn_auth[num] = (al_op, cs)
    for fid, d in live_data.get("details", {}).items():
        cs = (d.get("callsign") or "").upper()
        num = (d.get("number_default") or "").upper()
        al_op = (d.get("airline_icao") or d.get("operator_query") or "").upper()
        if num and cs and al_op in TARGET_OPS:
            fn_auth[num] = (al_op, cs)
        if d.get("origin_iata") and d.get("origin_icao"):
            iata_to_icao.setdefault(d["origin_iata"], d["origin_icao"])
        if d.get("dest_iata") and d.get("dest_icao"):
            iata_to_icao.setdefault(d["dest_iata"], d["dest_icao"])

    for actual_pass in (True, False):
        for ap_icao, payload in ap_data.items():
            for r in payload.get("rows", []):
                if r.get("origin_iata") and r.get("origin_icao"):
                    iata_to_icao.setdefault(r["origin_iata"], r["origin_icao"])
                if r.get("dest_iata") and r.get("dest_icao"):
                    iata_to_icao.setdefault(r["dest_iata"], r["dest_icao"])
                is_act = row_is_actual(r)
                if is_act != actual_pass:
                    continue
                cs = (r.get("callsign") or "").upper()
                num = (r.get("number") or "").upper()
                ac_code = (r.get("aircraft_code") or "").upper()
                if not num:
                    continue
                if is_cao_placeholder_row(cs, num, ac_code, is_act, cao_flight_nums, by_flight_search):
                    fn_auth.setdefault(num, ("CAO", ("CAO" + num[2:]) if num.startswith("CA") else num))
                elif cs.startswith(TARGET_OPS):
                    fn_auth.setdefault(num, (cs[:3], cs))

    for actual_pass in (True, False):
        for fl_num, payload in fh_data.items():
            for r in payload.get("rows", []):
                if r.get("origin_iata") and r.get("origin_icao"):
                    iata_to_icao.setdefault(r["origin_iata"], r["origin_icao"])
                if r.get("dest_iata") and r.get("dest_icao"):
                    iata_to_icao.setdefault(r["dest_iata"], r["dest_icao"])
                if str(r.get("status_generic") or "").lower() in ("canceled", "diverted"):
                    continue
                ev_ts = r.get("real_dep") or r.get("sched_dep") or r.get("real_arr") or r.get("sched_arr")
                if not ev_ts or int(ev_ts) < WINTER_START_TS:
                    continue
                is_act = row_is_actual(r)
                if is_act != actual_pass:
                    continue
                raw_al = (r.get("airline_icao") or "").upper()
                cs = (r.get("callsign") or "").upper()
                num = (r.get("number") or fl_num or "").upper()
                ac_code = (r.get("aircraft_code") or "").upper()
                if not num or (raw_al and raw_al not in TARGET_OPS):
                    continue
                if cs and len(cs) >= 3 and cs[:3].isalpha() and not cs.startswith(TARGET_OPS):
                    continue
                if is_cao_placeholder_row(cs, num, ac_code, is_act, cao_flight_nums, by_flight_search):
                    fn_auth.setdefault(num, ("CAO", ("CAO" + num[2:]) if num.startswith("CA") else num))
                elif cs.startswith(TARGET_OPS):
                    fn_auth.setdefault(num, (cs[:3], cs))

    for fl_num, item in by_flight_search.items():
        det = item.get("detail") or {}
        op = (det.get("operator") or "").upper()
        cs = (det.get("callsign") or "").upper()
        f_up = fl_num.upper()
        if op in TARGET_OPS and f_up not in fn_auth:
            if (op == "CAO" or f_up in cao_flight_nums) and (not cs or cs.startswith("CCA")):
                fn_auth[f_up] = ("CAO", ("CAO" + f_up[2:]) if f_up.startswith("CA") else f_up)
            elif cs:
                fn_auth[f_up] = (op, cs)
            else:
                fn_auth[f_up] = (op, (op + f_up[2:]) if f_up.startswith("CA") else f_up)

    observations = []
    seen = set()
    seen_ids = set()

    # 1. Live
    details_map = live_data.get("details", {})
    for op, fls in live_data.get("live", {}).items():
        for f in fls:
            fid = f["id"]
            d = details_map.get(fid)
            if d:
                al_op = (d.get("airline_icao") or op).upper()
                cs = (d.get("callsign") or "").upper()
                num = (d.get("number_default") or "").upper()
                orig = norm_ap(d.get("origin_icao"), d.get("origin_iata"))
                dest = norm_ap(d.get("dest_icao"), d.get("dest_iata"))
                tm = d.get("time") or {}
                s_dep = (tm.get("scheduled") or {}).get("departure")
                s_arr = (tm.get("scheduled") or {}).get("arrival")
                r_dep = (tm.get("real") or {}).get("departure")
                r_arr = (tm.get("real") or {}).get("arrival")
                ac = d.get("aircraft_code")
                reg = d.get("registration")
                st = d.get("status_text")
            else:
                al_op = (f.get("airline_icao") or op).upper()
                cs = (f.get("callsign") or "").upper()
                num = (f.get("number") or "").upper()
                orig = norm_ap(None, f.get("origin_iata"))
                dest = norm_ap(None, f.get("destination_iata"))
                s_dep = s_arr = r_dep = r_arr = None
                ac = f.get("aircraft_code")
                reg = f.get("registration")
                st = "live_feed"
            if orig and dest and ("live", fid) not in seen:
                seen.add(("live", fid))
                if fid:
                    seen_ids.add(fid)
                ev_ts = r_dep or s_dep or r_arr or s_arr or f.get("time") or NOV_START_TS
                observations.append({
                    "source": "live", "id": fid, "op": al_op, "op_source": "live",
                    "callsign": cs or (al_op + num[2:] if num.startswith("CA") else num),
                    "number": num, "orig": orig, "dest": dest, "ac": ac, "reg": reg,
                    "sched_dep": s_dep, "sched_arr": s_arr, "real_dep": r_dep, "real_arr": r_arr,
                    "status": st, "is_actual": True,
                    "ev_ts": int(ev_ts) if ev_ts else None, "is_live_or_fut_sched": True,
                })

    # 2. Aircraft history
    for fid, d in details_map.items():
        base_op = (d.get("airline_icao") or d.get("operator_query") or "").upper()
        for h in d.get("history") or []:
            h_id = h.get("id")
            h_num = (h.get("number") or "").upper()
            h_orig = norm_ap(h.get("origin_icao"), h.get("origin_iata"))
            h_dest = norm_ap(h.get("dest_icao"), h.get("dest_iata"))
            h_tm = h.get("time") or {}
            h_rdep = (h_tm.get("real") or {}).get("departure")
            h_sdep = (h_tm.get("scheduled") or {}).get("departure")
            h_sarr = (h_tm.get("scheduled") or {}).get("arrival")
            h_rarr = (h_tm.get("real") or {}).get("arrival")
            if h_num.startswith(TARGET_OPS):
                h_op, h_cs = h_num[:3], h_num
            elif h_num in fn_auth:
                h_op, h_cs = fn_auth[h_num]
            elif h_num.startswith("CA") and base_op in TARGET_OPS:
                h_op, h_cs = base_op, base_op + h_num[2:]
            else:
                h_op, h_cs = base_op, h_num
            dk = ("hist", h_id or (h_num, h_orig, h_dest, h_rdep))
            if dk not in seen and h_orig and h_dest:
                seen.add(dk)
                if h_id:
                    seen_ids.add(h_id)
                ev_ts = h_rdep or h_sdep or h_rarr or h_sarr
                observations.append({
                    "source": "aircraft_history", "id": h_id, "op": h_op, "op_source": "history",
                    "callsign": h_cs, "number": h_num, "orig": h_orig, "dest": h_dest,
                    "ac": d.get("aircraft_code"), "reg": d.get("registration"),
                    "sched_dep": h_sdep, "sched_arr": h_sarr, "real_dep": h_rdep, "real_arr": h_rarr,
                    "status": "history", "is_actual": bool(h_rdep or h_rarr),
                    "ev_ts": int(ev_ts) if ev_ts else None, "is_live_or_fut_sched": True,
                })

    # 3. Airport boards
    actual_cargo_cca_rows = []
    for ap_icao, payload in ap_data.items():
        for r in payload.get("rows", []):
            info_c = classify_fr24_row(r, None, fn_auth, cao_flight_nums, by_flight_search)
            if not info_c:
                continue
            if info_c["is_actual_cao_cca"]:
                actual_cargo_cca_rows.append((ap_icao, info_c["num"], info_c["cs"], info_c["ac_code"], info_c["s_op"], r.get("origin_icao") or r.get("origin_iata"), r.get("dest_icao") or r.get("dest_iata")))

            orig = norm_ap(r.get("origin_icao"), r.get("origin_iata"))
            dest = norm_ap(r.get("dest_icao"), r.get("dest_iata"))
            if not orig or not dest:
                continue

            fid = r.get("id")
            dk = ("ap_id", fid) if fid else ("ap_sched", info_c["cs"] or info_c["num"], orig, dest, r.get("sched_dep"))
            if dk not in seen:
                seen.add(dk)
                if fid:
                    seen_ids.add(fid)
                ev_ts = r.get("real_dep") or r.get("sched_dep") or r.get("real_arr") or r.get("sched_arr")
                observations.append({
                    "source": f"airport:{ap_icao}", "id": fid, "op": info_c["op"], "op_source": info_c["op_source"],
                    "callsign": info_c["cs"], "number": info_c["num"], "orig": orig, "dest": dest,
                    "ac": r.get("aircraft_code"), "reg": r.get("registration"),
                    "sched_dep": r.get("sched_dep"), "sched_arr": r.get("sched_arr"),
                    "real_dep": r.get("real_dep"), "real_arr": r.get("real_arr"),
                    "status": r.get("status_text"), "is_actual": info_c["is_actual"],
                    "ev_ts": int(ev_ts) if ev_ts else None, "is_live_or_fut_sched": True,
                })

    # 4. Gold 365-day flight_histories.json: split into Winter (>= 2026-10-25 BJT) & Pre-Winter (< 2026-10-25 BJT)
    gold_winter_added = 0
    gold_prewinter_rows = 0
    pre_by_op_cs_od = defaultdict(list)
    pre_by_num_od = defaultdict(list)
    pre_by_cs = defaultdict(list)

    for fl_num, payload in fh_data.items():
        for r in payload.get("rows", []):
            st_gen = str(r.get("status_generic") or "").lower()
            if st_gen in ("canceled", "diverted"):
                continue
            info_c = classify_fr24_row(r, fl_num, fn_auth, cao_flight_nums, by_flight_search)
            if not info_c:
                continue
            orig = norm_ap(r.get("origin_icao"), r.get("origin_iata"))
            dest = norm_ap(r.get("dest_icao"), r.get("dest_iata"))
            if not orig or not dest:
                continue
            ev_ts = r.get("real_dep") or r.get("sched_dep") or r.get("real_arr") or r.get("sched_arr")
            if not ev_ts:
                continue
            ev_ts = int(ev_ts)
            if ev_ts >= WINTER_START_TS:
                fid = r.get("id")
                if fid and fid in seen_ids:
                    continue
                dk = ("ap_sched", info_c["cs"] or info_c["num"], orig, dest, r.get("sched_dep"))
                if not fid and dk in seen:
                    continue
                if fid:
                    seen_ids.add(fid)
                seen.add(dk)
                gold_winter_added += 1
                observations.append({
                    "source": f"gold:{fl_num}", "id": fid, "op": info_c["op"], "op_source": info_c["op_source"],
                    "callsign": info_c["cs"], "number": info_c["num"], "orig": orig, "dest": dest,
                    "ac": r.get("aircraft_code"), "reg": r.get("registration"),
                    "sched_dep": r.get("sched_dep"), "sched_arr": r.get("sched_arr"),
                    "real_dep": r.get("real_dep"), "real_arr": r.get("real_arr"),
                    "status": r.get("status_text"), "is_actual": info_c["is_actual"],
                    "ev_ts": ev_ts,
                    "is_live_or_fut_sched": bool(info_c["is_actual"] or st_gen == "scheduled" or ev_ts >= ACTIVE_SCHED_CUTOFF_TS),
                })
            else:
                gold_prewinter_rows += 1
                rec = {
                    "op": info_c["op"], "cs": info_c["cs"], "num": info_c["num"], "op_source": info_c["op_source"],
                    "orig": orig, "dest": dest, "ts": ev_ts, "is_actual": info_c["is_actual"], "ac": r.get("aircraft_code"),
                }
                pre_by_op_cs_od[(info_c["op"], info_c["cs"], orig, dest)].append(rec)
                if info_c["num"]:
                    pre_by_num_od[(info_c["num"], orig, dest)].append(rec)
                if info_c["cs"]:
                    pre_by_cs[info_c["cs"]].append(rec)

    print(f"Airports loaded: {len(ap_data)} | Search flights: {len(by_flight_search)} | Gold flights cached: {len(fh_data)} (+{gold_winter_added} winter obs, {gold_prewinter_rows} pre-winter)")
    print(f"Unified Winter obs: {len(observations)}")
    print("  obs by op:", Counter(o["op"] for o in observations))
    print("  obs by op_source:", Counter(o["op_source"] for o in observations))
    print("  obs actual vs scheduled:", Counter(o["is_actual"] for o in observations))
    print(f"  actual rows with CCA callsign on cargo/CAO flight ({len(actual_cargo_cca_rows)}):", actual_cargo_cca_rows[:10])

    by_op_cs_od = defaultdict(list)
    by_num_od = defaultdict(list)
    by_cs = defaultdict(list)
    for o in observations:
        by_op_cs_od[(o["op"], o["callsign"], o["orig"], o["dest"])].append(o)
        if o["number"]:
            by_num_od[(o["number"], o["orig"], o["dest"])].append(o)
        if o["callsign"]:
            by_cs[o["callsign"]].append(o)

    def match_od(op, cs, orig, dest, num=None):
        o2 = ICAO_ALIASES.get(orig, orig)
        d2 = ICAO_ALIASES.get(dest, dest)
        exact = by_op_cs_od.get((op, cs, orig, dest), [])
        alias = by_op_cs_od.get((op, cs, o2, d2), []) if (o2 != orig or d2 != dest) else []
        cands = exact or alias
        if cands and num and all(o["op_source"] == "airport_iata_fallback" for o in cands):
            if num in fn_auth and fn_auth[num][0] != op:
                return [], [], []
        return exact, alias, cands

    def pre_winter_summary(op, cs, num, orig, dest):
        o2 = ICAO_ALIASES.get(orig, orig)
        d2 = ICAO_ALIASES.get(dest, dest)
        exact_pre = pre_by_op_cs_od.get((op, cs, orig, dest), []) + pre_by_op_cs_od.get((op, cs, o2, d2), [])
        exact_act = [x for x in exact_pre if x["is_actual"]]
        if exact_act:
            last_ts = max(x["ts"] for x in exact_act)
            return "pre_winter_stopped", {"last_date": fmt_date(last_ts), "n_pre_act": len(exact_act), "n_pre": len(exact_pre)}
        other_pre_op = [x for x in (pre_by_num_od.get((num, orig, dest), []) + pre_by_num_od.get((num, o2, d2), []))
                        if x["op"] != op and x["op_source"] != "airport_iata_fallback" and x["is_actual"]] if num else []
        if other_pre_op:
            last_ts = max(x["ts"] for x in other_pre_op)
            return "pre_winter_op_mismatch", {"last_date": fmt_date(last_ts), "pre_ops": sorted({(x["op"], x["cs"]) for x in other_pre_op})}
        other_pre_rt = [x for x in pre_by_cs.get(cs, []) if x["is_actual"]]
        if other_pre_rt:
            last_ts = max(x["ts"] for x in other_pre_rt)
            return "pre_winter_other_route", {"last_date": fmt_date(last_ts), "pre_ods": sorted({(x["orig"], x["dest"]) for x in other_pre_rt})}
        if num and num in fh_data:
            return "zero_in_365d", {"cached_rows": len(fh_data[num].get("rows", [])), "pre_sched_only": len(exact_pre)}
        return "unqueried", {}

    def probe_fr24(op, cs, num, orig, dest):
        """Unified FR24 lookup across Winter observations, Gold 365-day pre-winter history, and search index."""
        exact, alias, cands = match_od(op, cs, orig, dest, num=num)
        if cands:
            acts = [o for o in cands if o["is_actual"]]
            active_sched = [o for o in cands if o.get("is_live_or_fut_sched")]
            nov_acts = sum(1 for o in acts if (o.get("ev_ts") or 0) >= NOV_START_TS)
            if not acts and not active_sched:
                pw_kind, pw_info = pre_winter_summary(op, cs, num, orig, dest)
                stale_ts = [o["ev_ts"] for o in cands if o.get("ev_ts")]
                return "stale_sched_only", cands, {
                    "acts": 0, "stale_n": len(cands),
                    "max_stale_date": fmt_date(max(stale_ts)) if stale_ts else None,
                    "pre_kind": pw_kind, "pre_info": pw_info,
                }
            if alias and not exact:
                return "alias", cands, {"acts": len(acts), "nov_acts": nov_acts, "fr_od": sorted({(o["orig"], o["dest"]) for o in alias})}
            return ("direct_actual" if acts else "direct_sched"), cands, {"acts": len(acts), "nov_acts": nov_acts}

        o2 = ICAO_ALIASES.get(orig, orig)
        d2 = ICAO_ALIASES.get(dest, dest)
        same_num_od = (by_num_od.get((num, orig, dest), []) + by_num_od.get((num, o2, d2), [])) if num else []
        other_op = [o for o in same_num_od if o["op"] != op and o["op_source"] != "airport_iata_fallback"]
        if other_op:
            return "op_mismatch", [], sorted({(o["op"], o["callsign"], o["source"], o["op_source"]) for o in other_op})

        other_rt = by_cs.get(cs, [])
        if other_rt:
            return "route_mismatch", [], sorted({(o["orig"], o["dest"], o["is_actual"]) for o in other_rt})

        # Check Gold 365-day pre-winter records (< 2026-10-25 BJT) before treating search index as active
        pw_kind, pw_info = pre_winter_summary(op, cs, num, orig, dest)
        s_item = by_cs_search.get(cs) or (by_flight_search.get(num) if num else None)
        if pw_kind in ("pre_winter_stopped", "pre_winter_op_mismatch", "pre_winter_other_route"):
            return pw_kind, [], pw_info
        if pw_kind == "zero_in_365d":
            return "zero_in_365d", [], {**pw_info, "in_search": bool(s_item)}

        if s_item:
            s_op = ((s_item.get("detail") or {}).get("operator") or "").upper()
            s_cs = ((s_item.get("detail") or {}).get("callsign") or "").upper()
            if s_op and s_op != op and not (op == "CAO" and s_op == "CCA" and not s_cs.startswith("CCA")):
                return "search_op_mismatch", [], [(s_op, s_cs, "search")]
            return "search_only", [], [(s_op or op, s_cs, "search")]
        return "not_found", [], []

    def is_domestic_od(orig, dest):
        return bool(orig and dest and orig.startswith("Z") and dest.startswith("Z"))

    def ap_cov(orig, dest):
        o2 = ICAO_ALIASES.get(orig, orig)
        d2 = ICAO_ALIASES.get(dest, dest)
        return (o2 in ap_data), (d2 in ap_data)

    # 1. Winter-spring plan groups (2170) & 1(a) 10 single-day (2026-10-25) expired plans
    seg_groups = defaultdict(list)
    for r in cat["seg"]:
        seg_groups[(r["航司"], r["源呼号"], r["始发"], r["到达"])].append(r)

    seg_status = Counter()
    seg_op_mismatch = []
    seg_route_mismatch = []
    seg_alias_matched = []
    seg_time_diffs = []
    seg_not_found = []
    expired_results = []
    ac_map = defaultdict(Counter)

    seg_pre_stopped = []
    seg_zero_365 = []
    seg_oct_only_act = []

    for (op, cs, orig, dest), srows in seg_groups.items():
        num_guess = ("CA" + cs[3:]) if cs.startswith(TARGET_OPS) else cs
        kind, cands, info = probe_fr24(op, cs, num_guess, orig, dest)

        if kind == "alias":
            seg_status["机场旧代码别名匹配(换季后在飞/排班)"] += 1
            seg_alias_matched.append((op, cs, orig, dest, ICAO_ALIASES.get(orig, orig), ICAO_ALIASES.get(dest, dest), len(cands), info["acts"]))
        elif kind == "direct_actual":
            seg_status["换季后实飞确认(>=10/25)"] += 1
            if info.get("nov_acts", 0) == 0:
                last_ev = max((o["ev_ts"] for o in cands if o["is_actual"] and o.get("ev_ts")), default=None)
                has_fut = any(o.get("is_live_or_fut_sched") and not o["is_actual"] for o in cands)
                seg_oct_only_act.append((op, cs, orig, dest, info["acts"], fmt_dt(last_ev), has_fut))
        elif kind == "direct_sched":
            seg_status["换季后排班确认(>=10/25)"] += 1
        elif kind == "op_mismatch":
            seg_status["承运人不一致(FR24换季后归属另一家)"] += 1
            seg_op_mismatch.append((op, cs, orig, dest, info))
        elif kind == "route_mismatch":
            seg_status["同呼号但FR24换季后航段不同"] += 1
            seg_route_mismatch.append((op, cs, orig, dest, [x[:2] for x in info]))
        elif kind in ("pre_winter_stopped", "pre_winter_op_mismatch", "pre_winter_other_route"):
            seg_status["仅夏秋(<10/25)有飞、换季后停飞"] += 1
            seg_pre_stopped.append((op, cs, orig, dest, kind, info))
        elif kind == "zero_in_365d":
            seg_status["近365天0执飞记录(Gold全库验证)"] += 1
            seg_zero_365.append((op, cs, orig, dest, srows[0]["计划类别"], srows[0]["源班期"], info))
        elif kind == "search_op_mismatch":
            seg_status["FR24搜索承运人不一致"] += 1
            seg_op_mismatch.append((op, cs, orig, dest, info))
        elif kind == "search_only":
            seg_status["未在窗口观测(FR24搜索有号)"] += 1
        else:
            seg_status["FR24未观测且搜索无计划"] += 1
            seg_not_found.append((op, cs, orig, dest, srows[0]["计划类别"], srows[0]["源班期"], ap_cov(orig, dest)))

        non_exp = [sr for sr in srows if sr["处理组"] != "已过期"]
        if cands:
            for o in cands:
                if o["ac"]:
                    for sr in srows:
                        ac_map[(op, str(sr["源机型"]))][o["ac"]] += 1
            sched_obs = [o for o in cands if o["sched_dep"]]
            nov_sched = [o for o in sched_obs if int(o["sched_dep"]) >= NOV_START_TS]
            eval_sched = nov_sched or sched_obs
            if eval_sched and non_exp:
                plan_deps = sorted({str(sr["推算UTC起飞"]) for sr in non_exp if sr.get("推算UTC起飞")})
                fr_deps = sorted({fmt_hm(o["sched_dep"]) for o in eval_sched})
                if plan_deps and not (set(plan_deps) & set(fr_deps)):
                    best_diff = min(
                        (hhmm_diff_min(fd, pd) for fd in fr_deps for pd in plan_deps),
                        key=lambda d: abs(d),
                    )
                    if abs(best_diff) > 15:
                        seg_time_diffs.append({
                            "op": op, "cs": cs, "orig": orig, "dest": dest,
                            "cat": non_exp[0]["计划类别"], "n_rows": len(non_exp),
                            "plan_utc_deps": plan_deps, "fr_utc_deps": fr_deps, "diff_min": best_diff,
                        })

        for exp_sr in (sr for sr in srows if sr["处理组"] == "已过期"):
            fr_deps = sorted({fmt_hm(o["sched_dep"]) for o in cands if o["sched_dep"]})
            fr_arrs = sorted({fmt_hm(o["sched_arr"]) for o in cands if o["sched_arr"]})
            fr_acs = sorted({o["ac"] for o in cands if o.get("ac")})
            expired_results.append({
                "op": op, "cs": cs, "orig": orig, "dest": dest,
                "has_active_winter_row": bool(non_exp),
                "plan_dep": exp_sr.get("推算UTC起飞"), "plan_arr": exp_sr.get("推算UTC到达"),
                "plan_ac": exp_sr.get("源机型"), "plan_days": exp_sr.get("源班期"),
                "fr24_kind": kind, "n_obs": len(cands),
                "n_actual": sum(1 for o in cands if o["is_actual"]),
                "fr_deps": fr_deps, "fr_arrs": fr_arrs, "fr_acs": fr_acs,
                "pre_info": info if isinstance(info, dict) else {},
                "search_hit": (by_cs_search.get(cs) or by_flight_search.get(num_guess) or {}).get("detail"),
            })

    print("\n=== 1. 冬春航段组核查 (2170 groups) ===")
    print("  status:", seg_status)
    print(f"  其中换季后实飞仅见10/25–10/31、11月后0实飞 ({len(seg_oct_only_act)}):", seg_oct_only_act)
    print(f"  机场旧代码别名匹配 ({len(seg_alias_matched)}):", seg_alias_matched)
    print(f"  承运人不一致 ({len(seg_op_mismatch)}):")
    for x in seg_op_mismatch:
        print("   ", x)
    print(f"  同呼号航段不同 ({len(seg_route_mismatch)}):")
    for x in seg_route_mismatch:
        print("   ", x)
    print(f"  仅夏秋(<10/25)有飞、换季后停飞 ({len(seg_pre_stopped)}):")
    for x in seg_pre_stopped:
        print("   ", x)
    print(f"  近365天0执飞记录 ({len(seg_zero_365)}):")
    for x in seg_zero_365[:30]:
        print("   ", x)
    print(f"  FR24未观测且搜索无计划 ({len(seg_not_found)}):")
    for x in seg_not_found:
        print("   ", x)
    print(f"  冬春航段组起飞时刻偏差 > 15min ({len(seg_time_diffs)}):")
    for x in sorted(seg_time_diffs, key=lambda d: -abs(d["diff_min"]))[:25]:
        print("   ", x)
    print(f"\n=== 1(a). 10条仅10/25单日已过期计划核对 ({len(expired_results)}) ===")
    for x in expired_results:
        s_op = (x["search_hit"] or {}).get("operator") if x["search_hit"] else None
        last_d = (x["pre_info"] or {}).get("last_date")
        print(f"  {x['cs']} {x['orig']}->{x['dest']} | 同OD另有冬春行={x['has_active_winter_row']} | kind={x['fr24_kind']} obs={x['n_obs']} act={x['n_actual']} last_pre={last_d} | plan={x['plan_dep']}-{x['plan_arr']}[{x['plan_ac']}] | fr_dep={x['fr_deps']} ac={x['fr_acs']} | search_op={s_op}")

    # 2. 拟停用 (590 groups) & 140 优先队列 & 22保留 & 6不在比较范围
    rev_map = {(r["航司"], r["呼号"], r["始发"], r["到达"]): r for r in cat["rev"]}
    stop_status = Counter()
    stop_still_active = []
    stop_stale_sched = []
    stop_op_swapped = []
    stop_rt_swapped = []
    stop_pre_stopped = []
    stop_not_found = []
    stop_all_rows = []
    stop_cross_table = Counter()
    prio_status = Counter()
    prio_rows = []

    TAG_MAP = {
        "direct_actual": "FR24换季后仍实飞(>=10/25)",
        "direct_sched": "FR24换季后仍排班(>=10/25)",
        "stale_sched_only": "换季初仅见未飞占位排班(11/04前消失)",
        "alias": "FR24别名机场仍在飞/排班",
        "op_mismatch": "FR24换季后确认换承运人",
        "route_mismatch": "FR24换季后确认换航段",
        "pre_winter_stopped": "Gold确认夏秋有飞·10/25后已停飞",
        "pre_winter_op_mismatch": "Gold确认10/25前他司飞·后停飞",
        "pre_winter_other_route": "Gold确认10/25前仅飞他段·后停飞",
        "zero_in_365d": "Gold确认近365天0执飞记录",
        "search_op_mismatch": "FR24搜索确认换承运人",
        "search_only": "未入Gold窗口(搜索有号)",
        "not_found": "未观测且搜索无该号",
    }

    for r in cat["stop"]:
        op, cs, orig, dest = r["航司"], r["现有呼号"], r["始发"], r["到达"]
        num = r["现有班号"]
        rev_row = rev_map.get((op, cs, orig, dest), {})
        old_ev = rev_row.get("证据与处置建议", "未知")
        is_prio = (rev_row.get("原优先清单") == "是")

        kind, cands, info = probe_fr24(op, cs, num, orig, dest)
        tag = TAG_MAP[kind]

        if kind in ("direct_actual", "direct_sched", "alias"):
            acts = [o for o in cands if o["is_actual"]]
            nov_acts = info.get("nov_acts", 0)
            alt_rows = [o for o in by_num_od.get((num, orig, dest), []) if o["op"] != op]
            alt_acts = sum(1 for o in alt_rows if o["is_actual"])
            alt_total = sum(1 for o in alt_rows if o.get("is_live_or_fut_sched"))
            is_swap_overlap = bool(r["可能相关的冬春航段"] and len(acts) <= 2 and (alt_acts >= 5 or alt_total >= 10))
            sub_tag = "偶发代飞交替(主承运已切冬春)" if is_swap_overlap else ("持续实飞(>=10/25)" if acts else "近期仍有未来排班")
            last_act = max((int(o["real_dep"] or o["sched_dep"] or o["real_arr"] or o["sched_arr"]) for o in acts if (o.get("real_dep") or o.get("sched_dep") or o.get("real_arr") or o.get("sched_arr"))), default=None)
            stop_still_active.append({
                "op": op, "cs": cs, "orig": orig, "dest": dest, "tag": tag, "sub_tag": sub_tag,
                "is_swap_overlap": is_swap_overlap, "alt_acts": alt_acts,
                "n_obs": len(cands), "n_actual": len(acts), "nov_actuals": nov_acts,
                "last_act_date": fmt_date(last_act),
                "acs": sorted({o["ac"] for o in cands if o.get("ac")}),
                "fr_deps": sorted({fmt_hm(o["sched_dep"]) for o in cands if o.get("sched_dep")}),
                "old_adsb": old_ev, "prio": is_prio, "ids": r["涉及VAMSYS航线ID"], "alt": r["可能相关的冬春航段"],
            })
        elif kind == "stale_sched_only":
            stop_stale_sched.append({
                "op": op, "cs": cs, "orig": orig, "dest": dest, "stale_n": info["stale_n"],
                "max_stale_date": info["max_stale_date"], "pre_kind": info["pre_kind"],
                "pre_last": (info.get("pre_info") or {}).get("last_date"),
                "old_adsb": old_ev, "prio": is_prio, "ids": r["涉及VAMSYS航线ID"],
            })
        elif kind in ("op_mismatch", "search_op_mismatch"):
            stop_op_swapped.append((op, cs, orig, dest, is_prio, old_ev, info))
        elif kind == "route_mismatch":
            stop_rt_swapped.append((op, cs, orig, dest, is_prio, old_ev, [x[:2] for x in info]))
        elif kind in ("pre_winter_stopped", "pre_winter_op_mismatch", "pre_winter_other_route"):
            stop_pre_stopped.append((op, cs, orig, dest, kind, info, is_prio, old_ev))
        elif kind in ("zero_in_365d", "not_found"):
            stop_not_found.append({
                "op": op, "cs": cs, "num": num, "orig": orig, "dest": dest,
                "kind": kind, "info": info,
                "dom": is_domestic_od(orig, dest), "cov": ap_cov(orig, dest),
                "prio": is_prio, "old_adsb": old_ev, "ids": r["涉及VAMSYS航线ID"],
            })

        stop_status[tag] += 1
        stop_cross_table[(old_ev, tag)] += 1
        if is_prio:
            prio_status[tag] += 1
            prio_rows.append((op, cs, orig, dest, tag, old_ev, info if not cands else len(cands)))
        stop_all_rows.append({
            "op": op, "cs": cs, "num": num, "orig": orig, "dest": dest,
            "tag": tag, "old_adsb": old_ev, "prio": is_prio,
            "n_obs": len(cands), "ids": r["涉及VAMSYS航线ID"], "alt": r["可能相关的冬春航段"],
            "detail": str(info),
        })

    print("\n=== 2. 拟停用核对 (590 groups) ===")
    print("  status:", stop_status)
    print("  140优先队列核对结果:", prio_status, f"(total={sum(prio_status.values())})")
    sustained = [x for x in stop_still_active if not x["is_swap_overlap"]]
    overlaps = [x for x in stop_still_active if x["is_swap_overlap"]]
    print(f"  换季后(>=10/25)仍在飞/排班 ({len(stop_still_active)} = 持续在飞/排班 {len(sustained)} + 偶发代飞交接 {len(overlaps)}):")
    for x in sustained:
        print(f"    [{x['sub_tag']}] {x['op']} {x['cs']} {x['orig']}->{x['dest']} | obs={x['n_obs']},act={x['n_actual']},nov={x['nov_actuals']},last={x['last_act_date']},ac={x['acs']},dep={x['fr_deps']} | 原ADSB={x['old_adsb']} | 优先={x['prio']} | alt={x['alt']}")
    print(f"  其中旧承运偶发代飞交接 ({len(overlaps)}, 主承运已在冬春计划大量执飞):")
    for x in overlaps:
        print(f"    [交接] {x['op']} {x['cs']} {x['orig']}->{x['dest']} | act={x['n_actual']} last={x['last_act_date']} vs 新承运冬春act={x['alt_acts']} | alt={x['alt']}")
    print(f"  换季初仅见未飞占位排班(11/04前消失) ({len(stop_stale_sched)}):", Counter(x["pre_kind"] for x in stop_stale_sched))
    for x in stop_stale_sched[:10]:
        print("   ", x["op"], x["cs"], f"{x['orig']}->{x['dest']}", f"stale={x['stale_n']} max={x['max_stale_date']} pre={x['pre_kind']} last_pre={x['pre_last']}")

    print(f"\n  拟停用确认换承运人 ({len(stop_op_swapped)}):")
    for x in stop_op_swapped:
        print("   ", x)
    print(f"\n  拟停用确认换航段 ({len(stop_rt_swapped)}) [按原优先/ADSB分类]:", Counter((x[4], x[5]) for x in stop_rt_swapped))
    print(f"\n  Gold 确认夏秋有飞·10/25后停飞 ({len(stop_pre_stopped)}):")
    for x in stop_pre_stopped[:25]:
        print("   ", x[0], x[1], f"{x[2]}->{x[3]}", x[4], x[5], "prio=" + str(x[6]), "adsb=" + str(x[7]))
    print(f"\n  Gold 确认近365天0执飞 ({len(stop_not_found)}):")
    print("    by op:", Counter(x["op"] for x in stop_not_found), "kind:", Counter(x["kind"] for x in stop_not_found))
    for x in stop_not_found[:30]:
        print("   ", x["op"], x["cs"], x["orig"], "->", x["dest"], x["kind"], x["info"], "prio=" + str(x["prio"]))

    print("\n  交叉对比 (原ADSB.aero -> FR24 Gold):")
    for k, v in sorted(stop_cross_table.items()):
        print(f"    {k[0]}  ==>  {k[1]} : {v}")

    print("\n  原ADSB.aero 16组重点线索 vs FR24 (含Gold全季):")
    for r in cat["rev"]:
        ev = r["证据与处置建议"]
        if ev in ("近7日双端支持，暂缓停用", "季内曾观测，先核当前状态", "近场坐标线索，先核方向"):
            op, cs, orig, dest = r["航司"], r["呼号"], r["始发"], r["到达"]
            cands = by_op_cs_od.get((op, cs, orig, dest), [])
            acts = [o for o in cands if o["is_actual"]]
            last_act = max((int(o["real_dep"] or o["sched_dep"] or o["real_arr"] or o["sched_arr"]) for o in acts if (o.get("real_dep") or o.get("sched_dep") or o.get("real_arr") or o.get("sched_arr"))), default=None)
            other = [o for o in by_cs.get(cs, []) if (o["orig"], o["dest"]) != (orig, dest)]
            pre_same = pre_by_op_cs_od.get((op, cs, orig, dest), [])
            pre_last = fmt_date(max(x["ts"] for x in pre_same)) if pre_same else None
            print(f"    {cs} {orig}->{dest} ({ev}) | 冬春同向={len(cands)}(实飞={len(acts)},末次={fmt_date(last_act)}) | 10/25前={len(pre_same)}(末次={pre_last}) | 冬春他向={sorted({(o['orig'], o['dest']) for o in other})}")

    print("\n  现有库'保留'(22) 与 '不在比较范围'(6) 核对:")
    for grp_name in ("保留", "不在比较范围"):
        rows_g = [r for r in cat["cur"] if r.get("处理组") == grp_name]
        grp_cnt = Counter()
        grp_details = []
        for r in rows_g:
            op, cs, num, orig, dest = r["航司"], r["现有呼号"], str(r.get("现有班号") or ""), r["始发"], r["到达"]
            kind, cands, info = probe_fr24(op, cs, num, orig, dest)
            grp_cnt[kind] += 1
            grp_details.append((op, cs, orig, dest, r.get("差异/口径"), kind, len(cands), info if not cands else []))
        print(f"    [{grp_name} x{len(rows_g)}]:", grp_cnt)
        for gd in grp_details:
            if grp_name == "不在比较范围" or gd[5] not in ("direct_actual", "direct_sched"):
                print("     ", gd)

    # 3. 1750 组调整清单 UTC 时刻与差异全面核查
    adj_probe_status = Counter()
    adj_time_status = Counter()
    adj_dep_devs = []
    adj_arr_only_devs = []
    adj_vamsys_match_plan_diff = []
    adj_special_items = []

    for r in cat["adj"]:
        op, cs, orig, dest = r["航司"], r["呼号"], r["始发"], r["到达"]
        num = ("CA" + cs[3:]) if cs.startswith(TARGET_OPS) else str(r.get("现有班号") or cs)
        diff_type = str(r.get("差异") or "")
        kind, cands, info = probe_fr24(op, cs, num, orig, dest)
        adj_probe_status[kind] += 1

        if "现有呼号与班号数字不同" in diff_type or "现有时刻/班期空缺" in diff_type or kind in ("alias", "op_mismatch", "route_mismatch", "search_op_mismatch", "pre_winter_stopped", "zero_in_365d"):
            adj_special_items.append({
                "op": op, "cs": cs, "num": num, "orig": orig, "dest": dest,
                "diff_type": diff_type, "fr24_kind": kind,
                "cur_sched": r.get("现有UTC班期/时刻"), "plan_sched": r.get("推算UTC班期/时刻 [机型]"),
                "info": str(info), "obs_cnt": len(cands),
                "fr_deps": sorted({fmt_hm(o["sched_dep"]) for o in cands if o.get("sched_dep")}),
            })

        sched_obs = [o for o in cands if o.get("sched_dep")]
        if not sched_obs:
            continue

        nov_sched = [o for o in sched_obs if int(o["sched_dep"]) >= NOV_START_TS]
        eval_sched = nov_sched or sched_obs

        plan_slots = parse_hhmm_slots(r.get("推算UTC班期/时刻 [机型]"))
        cur_slots = parse_hhmm_slots(r.get("现有UTC班期/时刻"))
        fr_pairs = sorted({(fmt_hm(o["sched_dep"]) or "", fmt_hm(o.get("sched_arr")) or "") for o in eval_sched})
        fr_deps = sorted({p[0] for p in fr_pairs if p[0]})

        if not plan_slots:
            adj_time_status["冬春推算时刻空缺"] += 1
            continue

        # Best match across observed Nov+ (or Winter) FR24 departure vs planned departure slot
        dep_diffs = [hhmm_diff_min(fd, pd) for fd in fr_deps for (pd, _) in plan_slots]
        best_plan_dep = min(dep_diffs, key=lambda d: abs(d))
        cur_dep_diffs = [hhmm_diff_min(fd, cd) for fd in fr_deps for (cd, _) in cur_slots] if cur_slots else []
        best_cur_dep = min(cur_dep_diffs, key=lambda d: abs(d)) if cur_dep_diffs else None

        arr_diffs = [hhmm_diff_min(fa, pa) for (_, fa) in fr_pairs if fa for (_, pa) in plan_slots]
        best_plan_arr = min(arr_diffs, key=lambda d: abs(d)) if arr_diffs else None

        if abs(best_plan_dep) <= 15:
            if best_plan_arr is not None and abs(best_plan_arr) > 15:
                adj_time_status["起飞吻合(±15m)但到达偏差>15m"] += 1
                adj_arr_only_devs.append({
                    "op": op, "cs": cs, "orig": orig, "dest": dest, "dom": is_domestic_od(orig, dest),
                    "plan_slots": plan_slots, "cur_slots": cur_slots, "fr_pairs": fr_pairs,
                    "dep_diff": best_plan_dep, "arr_diff": best_plan_arr,
                })
            else:
                adj_time_status["起飞与到达均与冬春计划吻合(±15m)"] += 1
        elif best_cur_dep is not None and abs(best_cur_dep) <= 15:
            adj_time_status["起飞更吻合现行VAMSYS(与冬春计划偏差>15m)"] += 1
            item = {
                "op": op, "cs": cs, "orig": orig, "dest": dest, "dom": is_domestic_od(orig, dest),
                "diff_type": diff_type, "plan_slots": plan_slots, "cur_slots": cur_slots,
                "fr_pairs": fr_pairs, "plan_dep_diff": best_plan_dep, "cur_dep_diff": best_cur_dep,
                "match_bucket": "吻合现行VAMSYS",
            }
            adj_vamsys_match_plan_diff.append(item)
            adj_dep_devs.append(item)
        else:
            adj_time_status["起飞与冬春计划及VAMSYS均偏差>15m"] += 1
            adj_dep_devs.append({
                "op": op, "cs": cs, "orig": orig, "dest": dest, "dom": is_domestic_od(orig, dest),
                "diff_type": diff_type, "plan_slots": plan_slots, "cur_slots": cur_slots,
                "fr_pairs": fr_pairs, "plan_dep_diff": best_plan_dep, "cur_dep_diff": best_cur_dep,
                "match_bucket": "与两库均偏差",
            })

    print("\n=== 3. 调整清单全面核查 (1750 groups) ===")
    print("  FR24覆盖匹配状态:", adj_probe_status)
    print("  有FR24排班起飞时刻样本的UTC核对:", adj_time_status, f"(total={sum(adj_time_status.values())})")
    print(f"  起飞偏差>15min明细总数 ({len(adj_dep_devs)}) | 国内/国际:", Counter(d["dom"] for d in adj_dep_devs), "| 归属:", Counter(d["match_bucket"] for d in adj_dep_devs))
    for d in sorted(adj_dep_devs, key=lambda x: -abs(x["plan_dep_diff"]))[:25]:
        print("   ", d["op"], d["cs"], f"{d['orig']}->{d['dest']}", "dom=" + str(d["dom"]), d["match_bucket"], f"plan_diff={d['plan_dep_diff']}m", f"cur_diff={d['cur_dep_diff']}m", "plan=", d["plan_slots"], "cur=", d["cur_slots"], "fr=", d["fr_pairs"])
    print(f"  调整特殊项/异常项 ({len(adj_special_items)}):")
    for x in adj_special_items[:40]:
        print("   ", x["op"], x["cs"], f"{x['orig']}->{x['dest']}", x["fr24_kind"], x["diff_type"], "info=", x["info"])

    # 4. 新增 (392 groups)
    add_status = Counter()
    add_issues = []
    for r in cat["add"]:
        op, cs, orig, dest = r["航司"], r["计划呼号"], r["始发"], r["到达"]
        num = r["候选班号"]
        kind, cands, info = probe_fr24(op, cs, num, orig, dest)
        if kind == "alias":
            add_status["机场旧代码别名确认"] += 1
            add_issues.append(("alias", op, cs, orig, dest, info["fr_od"]))
        elif kind == "direct_actual":
            add_status["FR24换季后实飞确认(>=10/25)"] += 1
        elif kind == "direct_sched":
            add_status["FR24换季后排班确认(>=10/25)"] += 1
        elif kind == "op_mismatch":
            add_status["承运人不一致"] += 1
            add_issues.append(("op_mismatch", op, cs, orig, dest, [x[:2] for x in info]))
        elif kind == "route_mismatch":
            add_status["同呼号FR24飞其他航段"] += 1
            add_issues.append(("route_mismatch", op, cs, orig, dest, [x[:2] for x in info]))
        elif kind in ("pre_winter_stopped", "pre_winter_op_mismatch", "pre_winter_other_route"):
            add_status["仅夏秋(<10/25)有飞、换季后未飞"] += 1
            add_issues.append((kind, op, cs, orig, dest, info))
        elif kind == "zero_in_365d":
            add_status["近365天0执飞记录(Gold验证)"] += 1
            add_issues.append(("zero_in_365d", op, cs, orig, dest, info))
        elif kind == "search_op_mismatch":
            add_status["FR24搜索承运人不一致"] += 1
            add_issues.append(("search_op_mismatch", op, cs, orig, dest, info[0][0]))
        elif kind == "search_only":
            add_status["未入Gold窗口(搜索有号)"] += 1
        else:
            add_status["FR24未观测且搜索无号"] += 1

    print("\n=== 4. 新增核对 (392 groups) ===")
    print("  status:", add_status)
    for x in add_issues:
        print("   ", x)

    # 5. 机队对照
    print("\n=== 5. 机队对照 (计划源机型 -> FR24 实飞/排班 Aircraft Code) ===")
    focus_codes = {"32D", "31C", "33C", "33H", "32H", "73M", "73E", "73K", "73C", "330", "777", "350", "787", "747", "737", "320", "321", "919", "909", "C91", "ARJ"}
    ac_summary = {}
    for (op, src_ac), cnt in sorted(ac_map.items()):
        top = cnt.most_common()
        ac_summary[f"{op}:{src_ac}"] = dict(top)
        if src_ac in focus_codes or len(cnt) > 1 or src_ac in ("77F", "74F", "74Y", "33F", "33L", "33A", "33G", "789"):
            print(f"  {op} {src_ac:<5} -> {top}")

    # 6. 计划外活跃航段分类 (vs 冬春计划, VAMSYS, 原始 CAAC .xls)
    raw_blobs = {}
    if os.path.isdir(raw_plan_dir):
        for fn in sorted(os.listdir(raw_plan_dir)):
            if fn.endswith(".xls"):
                raw_blobs[fn] = open(os.path.join(raw_plan_dir, fn), "rb").read()

    def check_raw_xls(digits):
        hits = []
        if not digits:
            return hits
        for fn, data in raw_blobs.items():
            for pref in ("CCA", "CAO", "CCD", "BJN", "CA"):
                pat = (pref + digits).encode("ascii")
                pat16 = (pref + digits).encode("utf-16le")
                if pat in data or pat16 in data:
                    hits.append(f"{fn}:{pref}{digits}")
        return hits

    plan_keys_all = set(seg_groups.keys())
    plan_keys_with_alias = set(plan_keys_all)
    for op, cs, orig, dest in plan_keys_all:
        plan_keys_with_alias.add((op, cs, ICAO_ALIASES.get(orig, orig), ICAO_ALIASES.get(dest, dest)))
    vamsys_keys = {(r["航司"], r["现有呼号"], r["始发"], r["到达"]) for r in cat["cur"]}

    unplanned = defaultdict(list)
    for o in observations:
        if o["op_source"] == "airport_iata_fallback" and o["number"] in fn_auth and fn_auth[o["number"]][0] != o["op"]:
            continue
        k = (o["op"], o["callsign"], o["orig"], o["dest"])
        if k not in plan_keys_with_alias:
            unplanned[k].append(o)

    in_vamsys = [k for k in unplanned if k in vamsys_keys]
    neither = [k for k in unplanned if k not in vamsys_keys]
    unplanned_cat = Counter()
    unplanned_rows = []

    for k in sorted(neither):
        op, cs, orig, dest = k
        obs_k = unplanned[k]
        acs = sorted({x["ac"] for x in obs_k if x.get("ac")})
        acts = sum(1 for x in obs_k if x["is_actual"])
        if acts == 0 and len(obs_k) < 2:
            continue
        digits = re.sub(r"^[A-Z]+", "", cs)
        raw_hits = check_raw_xls(digits)
        if raw_hits:
            cat_name = "A_原始CAAC表已收(多为承运人/代码差异)"
        elif orig == dest or digits.startswith("0") or len(digits) <= 2 or (op == "CCD" and digits.startswith("0")):
            cat_name = "B_训练试飞/调机/保障号段"
        elif op == "CAO":
            cat_name = "C_国货航CAO计划外货运航段"
        else:
            cat_name = "D_常规客运新增/未录计划航段"
        unplanned_cat[cat_name] += 1
        unplanned_rows.append({
            "cat": cat_name, "op": op, "cs": cs, "orig": orig, "dest": dest,
            "dom": is_domestic_od(orig, dest), "obs": len(obs_k), "act": acts,
            "ac": acs, "deps": sorted({fmt_hm(x["sched_dep"]) for x in obs_k if x.get("sched_dep")}),
            "raw_xls_hits": raw_hits[:3],
        })

    print(f"\n=== 6. 计划外活跃航段分类 ===")
    print(f"  在VAMSYS现有库(拟停用仍在飞/重号): {len(in_vamsys)} | 既不在冬春计划也不在VAMSYS: {len(neither)} | 其中实飞>=1或观测>=2: {len(unplanned_rows)}")
    print("  分类统计:", unplanned_cat)
    for cname in ("D_常规客运新增/未录计划航段", "A_原始CAAC表已收(多为承运人/代码差异)", "C_国货航CAO计划外货运航段", "B_训练试飞/调机/保障号段"):
        sub = [x for x in unplanned_rows if x["cat"] == cname]
        print(f"  [{cname} x{len(sub)}]:")
        for x in sub[:40]:
            print(f"    {x['op']} {x['cs']:<8} {x['orig']}->{x['dest']} obs={x['obs']} act={x['act']} ac={x['ac']} dep={x['deps']} raw={x['raw_xls_hits']}")

    # Diagnostic sanity check for specific callsigns (8627/8628/416)
    print("\n=== 7. 定点排查 (CA8627 / CA8628 / CA415 / CA416 / CA442 / CA620) ===")
    for num_check in ("CA8627", "CA8628", "CA415", "CA416", "CA442", "CA620"):
        hits = [o for o in observations if o["number"] == num_check]
        nov_hits = [o for o in hits if (o.get("ev_ts") or 0) >= NOV_START_TS]
        ods_w = sorted({(o["op"], o["orig"], o["dest"]) for o in hits})
        pre_hits = [r for k2, lst in pre_by_num_od.items() if k2[0] == num_check for r in lst]
        pre_acts = [r for r in pre_hits if r["is_actual"]]
        last_pre = fmt_date(max(x["ts"] for x in pre_acts)) if pre_acts else None
        max_w = fmt_dt(max((o["ev_ts"] for o in hits if o.get("ev_ts")), default=None))
        print(f"  {num_check}: winter_obs={len(hits)}(act={sum(1 for o in hits if o['is_actual'])},nov={len(nov_hits)},max={max_w},ods={ods_w}) pre_act={len(pre_acts)}(all={len(pre_hits)},last={last_pre})")

    # Save final JSON
    with open(f"{cache_dir}/final_audit.json", "w", encoding="utf-8") as f:
        json.dump({
            "airports_count": len(ap_data),
            "gold_flights_cached": len(fh_data),
            "observations_count": len(observations),
            "obs_by_op": dict(Counter(o["op"] for o in observations)),
            "obs_by_op_source": dict(Counter(o["op_source"] for o in observations)),
            "actual_cargo_cca_rows": actual_cargo_cca_rows,
            "seg_status": dict(seg_status),
            "seg_oct_only_act": seg_oct_only_act,
            "seg_op_mismatch": seg_op_mismatch,
            "seg_route_mismatch": seg_route_mismatch,
            "seg_alias_matched": seg_alias_matched,
            "seg_pre_stopped": seg_pre_stopped,
            "seg_zero_365": seg_zero_365,
            "seg_not_found": seg_not_found,
            "seg_time_diffs": seg_time_diffs,
            "expired_10_results": expired_results,
            "stop_status": dict(stop_status),
            "prio_status": dict(prio_status),
            "stop_cross_table": {f"{k[0]} => {k[1]}": v for k, v in stop_cross_table.items()},
            "stop_still_active": stop_still_active,
            "stop_stale_sched": stop_stale_sched,
            "stop_op_swapped": stop_op_swapped,
            "stop_rt_swapped": stop_rt_swapped,
            "stop_pre_stopped": stop_pre_stopped,
            "stop_not_found": stop_not_found,
            "adj_probe_status": dict(adj_probe_status),
            "adj_time_status": dict(adj_time_status),
            "adj_dep_devs": adj_dep_devs,
            "adj_arr_only_devs": adj_arr_only_devs,
            "adj_special_items": adj_special_items,
            "add_status": dict(add_status),
            "add_issues": add_issues,
            "unplanned_cat": dict(unplanned_cat),
            "unplanned_notable": unplanned_rows,
            "ac_map": ac_summary,
        }, f, ensure_ascii=False, indent=2)

    # Export multi-sheet Excel summary workbook
    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "核查概览"
    ws0.append(["核查模块", "指标项", "数量/结果"])
    ws0.append(["FR24采集范围", "覆盖机场数", len(ap_data)])
    ws0.append(["FR24采集范围", "Gold 365天单班历史缓存数", len(fh_data)])
    ws0.append(["FR24采集范围", "Search班号索引", len(by_flight_search)])
    ws0.append(["FR24采集范围", "换季后去重观测总条数(>=10/25)", len(observations)])
    for opk, opv in Counter(o["op"] for o in observations).items():
        ws0.append(["换季后观测分布", opk, opv])
    for k, v in seg_status.items():
        ws0.append(["冬春航段组(2170)", k, v])
    for k, v in stop_status.items():
        ws0.append(["拟停用(590)", k, v])
    for k, v in prio_status.items():
        ws0.append(["优先队列(140)", k, v])
    for k, v in adj_time_status.items():
        ws0.append(["调整清单UTC核对", k, v])
    for k, v in add_status.items():
        ws0.append(["新增清单(392)", k, v])
    for k, v in unplanned_cat.items():
        ws0.append(["计划外活跃航段", k, v])

    def add_sheet(title, headers, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for row in rows:
            ws.append([str(c) if isinstance(c, (list, dict, tuple, set)) else c for c in row])

    add_sheet(
        f"拟停用仍在飞{len(stop_still_active)}",
        ["航司", "呼号", "始发", "到达", "性质分类", "FR24结论", "观测数", "实飞数", "11月实飞", "新承运冬春实飞", "最后实飞日期", "FR24机型", "FR24起飞UTC", "原ADSB建议", "原优先队列", "VAMSYS航线ID", "可能相关冬春航段"],
        [[x["op"], x["cs"], x["orig"], x["dest"], x["sub_tag"], x["tag"], x["n_obs"], x["n_actual"], x["nov_actuals"], x["alt_acts"], x["last_act_date"], ",".join(x["acs"]), ",".join(x["fr_deps"]), x["old_adsb"], x["prio"], x["ids"], x["alt"]] for x in stop_still_active],
    )
    add_sheet(
        "拟停用全量590",
        ["航司", "呼号", "班号", "始发", "到达", "FR24结论", "原ADSB建议", "原优先队列", "换季后观测数", "VAMSYS航线ID", "可能相关冬春航段", "补充信息"],
        [[x["op"], x["cs"], x["num"], x["orig"], x["dest"], x["tag"], x["old_adsb"], x["prio"], x["n_obs"], x["ids"], x["alt"], x["detail"]] for x in stop_all_rows],
    )
    add_sheet(
        "调整起飞偏差",
        ["航司", "呼号", "始发", "到达", "国内", "结论归属", "冬春偏差(分)", "现行偏差(分)", "差异分类", "冬春计划UTC", "现行VAMSYS_UTC", "FR24观测UTC"],
        [[x["op"], x["cs"], x["orig"], x["dest"], x["dom"], x["match_bucket"], x["plan_dep_diff"], x["cur_dep_diff"], x["diff_type"], x["plan_slots"], x["cur_slots"], x["fr_pairs"]] for x in sorted(adj_dep_devs, key=lambda d: -abs(d["plan_dep_diff"]))],
    )
    add_sheet(
        "已过期单日10与计划异常",
        ["类别", "航司", "呼号", "始发", "到达", "状态/说明", "详情"],
        (
            [["仅10/25单日", x["op"], x["cs"], x["orig"], x["dest"], x["fr24_kind"], f"obs={x['n_obs']},act={x['n_actual']},last_pre={(x['pre_info'] or {}).get('last_date')},plan={x['plan_dep']}-{x['plan_arr']}[{x['plan_ac']}],fr_dep={x['fr_deps']},ac={x['fr_acs']}"] for x in expired_results]
            + [["冬春承运人不一致", x[0], x[1], x[2], x[3], "op_mismatch", str(x[4])] for x in seg_op_mismatch]
            + [["冬春同号不同航段", x[0], x[1], x[2], x[3], "route_mismatch", str(x[4])] for x in seg_route_mismatch]
            + [["冬春仅夏秋(<10/25)有飞", x[0], x[1], x[2], x[3], x[4], str(x[5])] for x in seg_pre_stopped]
            + [["冬春近365天0执飞", x[0], x[1], x[2], x[3], f"cat={x[4]},days={x[5]}", str(x[6])] for x in seg_zero_365]
            + [["冬春未观测无计划", x[0], x[1], x[2], x[3], "not_found", f"cat={x[4]},days={x[5]},cov={x[6]}"] for x in seg_not_found]
            + [["调整特殊异常", x["op"], x["cs"], x["orig"], x["dest"], x["diff_type"], f"kind={x['fr24_kind']},info={x['info']}"] for x in adj_special_items]
        ),
    )
    add_sheet(
        "机队实飞机型对照",
        ["航司", "计划源机型", "FR24实飞/排班机型分布"],
        [[k.split(":")[0], k.split(":")[1], str(v)] for k, v in sorted(ac_summary.items())],
    )
    add_sheet(
        "计划外活跃航段",
        ["分类", "航司", "呼号", "始发", "到达", "国内", "观测数", "实飞数", "FR24机型", "FR24起飞UTC", "原始CAAC表命中"],
        [[x["cat"], x["op"], x["cs"], x["orig"], x["dest"], x["dom"], x["obs"], x["act"], ",".join(x["ac"]), ",".join(x["deps"]), ",".join(x["raw_xls_hits"])] for x in unplanned_rows],
    )
    wb.save(out_xlsx)
    print(f"\nSaved audit JSON -> {cache_dir}/final_audit.json")
    print(f"Saved Excel detail -> {out_xlsx}")


if __name__ == "__main__":
    main()
