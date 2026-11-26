"""Standalone 2026-2027 Winter-Spring Plan vs VAMSYS (workspaces.db) + FR24 Gold 365-day rebuild.

Completely independent from prior prep spreadsheets:
  - Source 1: Raw CAAC pre-plan files (国内/国际/港澳台 (三字码).xls in SEASON_DIR)
  - Source 2: Current VAMSYS SQLite workspace (Data/workspaces.db, vCCA rev 484, read-only)
  - Source 3: FR24 Gold 365-day single-flight cache (1,332 flights) + 111 airport boards + live + search
"""

import datetime
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict

import openpyxl

from biff8_xls import read_biff8_sheets
from airport_grades import get_airport_grade, get_route_fleet_exclusions, route_allows_b752, route_min_grade
from fr24_paths import AP_CACHE_DIR, CACHE_DIR, SEASON_DIR, WORKSPACES_DB


TARGET_OPS = ("CCA", "CAO", "CCD", "BJN")
VAMSYS_WS_ID = "b8d41af1-5641-4b06-a476-b39829a63d25"
OUT_XLSX = os.path.join(SEASON_DIR, "2026-2027冬春航季_VAMSYS全新重算清单.xlsx")
OUT_JSON = os.path.join(CACHE_DIR, "rebuild_vamsys_summary.json")

DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_TO_IDX = {d: i for i, d in enumerate(DAY_NAMES)}

WINTER_START_TS = int(datetime.datetime(2026, 10, 24, 16, 0, tzinfo=datetime.timezone.utc).timestamp())  # 2026-10-25 00:00 BJT
NOV_START_TS = int(datetime.datetime(2026, 11, 1, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
ACTIVE_SCHED_CUTOFF_TS = int(datetime.datetime(2026, 11, 15, 0, 0, tzinfo=datetime.timezone.utc).timestamp())

# VAMSYS airport codes are authoritative (ZSGS, ZPMS, ZMCK, UZTT).
# Map FR24's divergent civil ICAO codes onto VAMSYS's airport codes.
FR24_TO_VAMSYS_AP = {"ZSJA": "ZSGS", "ZPLX": "ZPMS"}
CARGO_AC_CODES = {"77F", "B77F", "B77L", "77L", "74Y", "74F", "B74F", "33F"}

# VAMSYS Commercial Fleet Groups (ALL Param 9671/9677/9678/9679 aircraft regardless of active/retired status; excl. 26728 DH8D)
CCA_NB_3C = {"26102"}  # 3C: AJ27
CCA_NB_4C = CCA_NB_3C | {"26071", "26072", "26073", "26074", "26076", "26084", "26085", "26086", "26097"}  # 10 fleets (4C)
CCA_NB_4D = CCA_NB_4C | {"26090"}  # 11 fleets (+ B752, ICAO 4D wingspan 38.05m)

CCA_WB_4D = {"26091", "26092"}  # B762, B763 (ICAO 4D wingspan 47.57m; AC=0 in DB)
CCA_WB_4E = CCA_WB_4D | {
    "26077", "26078", "26079", "26080", "26087", "26088", "26093", "26094", "26095", "26591", "26594"
}  # 13 fleets (4D+4E: A332/A333/A339/A343/A346/A359/B742/B744/B762/B763/B772/B77W/B789)
CCA_WB_4F = CCA_WB_4E | {"26089"}  # 14 fleets (+ 4F-only B748)

CAO_ACTIVE = ["26722", "26724", "26725"]
CCD_ACTIVE = ["26719"]
BJN_ACTIVE = ["26718"]

EXCLUDED_DIRTY_FLEETS = {"26728"}  # 26728 DH8D (dirty cross-4-airline turboprop stub)
AIR_FORCE_FLEETS = {"27929", "27930", "27931", "27932", "29710", "29711"}
NB_SET = set(CCA_NB_4D)
WB_SET = set(CCA_WB_4F)

AC_TO_FLEET_PAX = {
    "319": "26071", "31C": "26071", "A319": "26071", "A19N": "26071",
    "320": "26072", "A320": "26072",
    "A20N": "26073", "32N": "26073", "32D": "26073", "32H": "26073",
    "321": "26074", "A321": "26074",
    "A21N": "26076", "32Q": "26076",
    "737": "26084", "73G": "26084", "733": "26084", "B737": "26084",
    "738": "26085", "73C": "26085", "73E": "26085", "73H": "26085", "73K": "26085", "73M": "26085", "B738": "26085",
    "7M8": "26086", "7M1": "26086", "B38M": "26086",
    "919": "26097", "C919": "26097",
    "ARJ": "26102", "909": "26102", "AJ27": "26102", "AR8": "26102",
    "752": "26090", "757": "26090", "B752": "26090",
    "762": "26091", "B762": "26091",
    "763": "26092", "767": "26092", "B763": "26092",
    "330": "26078", "332": "26077", "A332": "26077", "33C": "26077", "33H": "26077",
    "333": "26078", "A333": "26078",
    "339": "26591", "A339": "26591",
    "340": "26079", "343": "26079", "A343": "26079",
    "346": "26594", "A346": "26594",
    "350": "26080", "359": "26080", "A359": "26080",
    "742": "26087", "B742": "26087",
    "744": "26088", "B744": "26088",
    "748": "26089", "B748": "26089", "747": "26089",
    "772": "26093", "B772": "26093",
    "77W": "26094", "773": "26094", "777": "26094", "B77W": "26094", "B773": "26094",
    "787": "26095", "789": "26095", "B789": "26095",
}
AC_TO_FLEET_CARGO = {
    "77F": "26722", "B77F": "26722", "B77L": "26722", "77L": "26722",
    "74Y": "26724", "74F": "26724", "B744": "26724", "B74F": "26724",
    "33F": "26725", "A332": "26725", "332": "26725",
}


def parse_hhmm(val) -> int | None:
    s = str(val or "").strip()
    if not s:
        return None
    if ":" in s:
        p = s.split(":")
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            return int(p[0]) * 60 + int(p[1])
        return None
    if s.isdigit() and len(s) <= 4:
        s = s.zfill(4)
        return int(s[:2]) * 60 + int(s[2:])
    return None


def fmt_hm_min(m: int | None) -> str:
    if m is None:
        return ""
    m = m % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_hm_ts(ts) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%H:%M")


def fmt_date_ts(ts) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def circ_diff_min(m1: int, m2: int) -> int:
    return ((m1 - m2 + 720) % 1440) - 720


def hm_circ_diff(hm1: str, hm2: str) -> int:
    m1 = parse_hhmm(hm1)
    m2 = parse_hhmm(hm2)
    return circ_diff_min(m1, m2)


def fd3_to_day_set(fd3: str) -> set[int]:
    return {i for i, ch in enumerate(str(fd3 or "").strip()) if i < 7 and ch.isdigit()}


def vdays_to_day_set(vdays: str) -> set[int]:
    out = set()
    for part in str(vdays or "").lower().split(","):
        p = part.strip()
        if p in DAY_TO_IDX:
            out.add(DAY_TO_IDX[p])
    return out


def day_set_to_vdays(dset: set[int]) -> str:
    return ",".join(DAY_NAMES[i] for i in sorted(dset))


def day_set_to_fd3(dset: set[int]) -> str:
    return "".join(str(i + 1) if i in dset else "." for i in range(7))


def row_is_actual(r: dict) -> bool:
    return bool(
        r.get("real_dep")
        or r.get("real_arr")
        or r.get("status_live")
        or str(r.get("status_generic") or "").lower() in ("landed", "departed")
    )


def map_ac_to_fleet(op: str, ac_code: str) -> str | None:
    ac = str(ac_code or "").upper().strip()
    if op == "CAO":
        return AC_TO_FLEET_CARGO.get(ac)
    if op == "CCD":
        return "26719" if ac in ("738", "B738", "73C", "737", "B737") else AC_TO_FLEET_PAX.get(ac)
    if op == "BJN":
        return "26718"
    return AC_TO_FLEET_PAX.get(ac)


def fleet_codes_str(fids, v_fleets) -> str:
    codes = []
    for fid in sorted(fids, key=lambda x: (v_fleets.get(x, {}).get("Type Code", x), x)):
        tc = v_fleets.get(fid, {}).get("Type Code", fid)
        if v_fleets.get(fid, {}).get("Type (pax/cargo/...)") == "cargo":
            tc = tc + "(C)"
        codes.append(tc)
    return ",".join(codes)


def load_raw_preplan():
    files = [
        ("国内航司国内航班计划（三字码）.xls", "国内"),
        ("国内航司国际航班计划（三字码）.xls", "国际"),
        ("国内航司港澳台航班计划（三字码）.xls", "港澳"),
    ]
    raw_rows = []
    legs = []
    for fn, grp in files:
        fpath = os.path.join(SEASON_DIR, fn)
        sheets = read_biff8_sheets(fpath)
        for sname, tbl in sheets.items():
            hdr = tbl[0]
            for r_i, row in enumerate(tbl[1:], 2):
                if not row or not row[0]:
                    continue
                d = {hdr[i]: (row[i] if i < len(row) else "") for i in range(len(hdr))}
                cs = str(d["FD1"]).strip().upper()
                if not cs.startswith(TARGET_OPS):
                    continue
                op = cs[:3]
                num = "CA" + cs[3:]
                fd2 = str(d.get("FD2") or "").strip().upper()
                fd19 = str(d.get("FD19") or "").strip()
                fd23 = str(d.get("FD23") or "").strip().zfill(4)
                fd24 = str(d.get("FD24") or "").strip().zfill(4)
                fd26 = str(d.get("FD26") or "").strip().upper()
                bjt_days = fd3_to_day_set(d.get("FD3", ""))
                expired = (fd23 == "1025" and fd24 == "1025")
                raw_rows.append(d)

                # Expand cumulative BJT timeline across Leg 1 (FD4->FD7) and Leg 2 (FD7->FD10)
                o1 = str(d.get("FD4") or "").strip().upper()
                d1 = str(d.get("FD7") or "").strip().upper()
                m_dep1 = parse_hhmm(d.get("FD5"))
                m_arr1 = parse_hhmm(d.get("FD6"))
                day_off = 0
                if o1 and d1 and m_dep1 is not None and m_arr1 is not None:
                    utc_abs1 = m_dep1 - 480
                    utc_d1 = utc_abs1 % 1440
                    utc_a1 = (m_arr1 - 480) % 1440
                    d_shift1 = utc_abs1 // 1440  # -1 if < 08:00 BJT else 0
                    utc_days1 = {(day + d_shift1) % 7 for day in bjt_days}
                    legs.append({
                        "op": op, "cs": cs, "num": num, "orig": o1, "dest": d1,
                        "leg_no": 1, "is_multileg": bool(d.get("FD10")),
                        "bjt_days": str(d.get("FD3") or ""), "bjt_dep": fmt_hm_min(m_dep1), "bjt_arr": fmt_hm_min(m_arr1),
                        "utc_dep": fmt_hm_min(utc_d1), "utc_arr": fmt_hm_min(utc_a1),
                        "utc_dep_min": utc_d1, "utc_arr_min": utc_a1,
                        "utc_days": utc_days1, "fd2": fd2, "fd19": fd19, "fd26": fd26,
                        "fd23": fd23, "fd24": fd24, "expired": expired, "grp": grp,
                        "route_type": "cargo" if (op == "CAO" or fd26 == "F") else "scheduled",
                        "src_ref": f"{fn}:{r_i}",
                    })
                    if m_arr1 < m_dep1:
                        day_off += 1

                d2_ap = str(d.get("FD10") or "").strip().upper()
                if d1 and d2_ap:
                    m_dep2 = parse_hhmm(d.get("FD8"))
                    m_arr2 = parse_hhmm(d.get("FD9"))
                    if m_dep2 is not None and m_arr2 is not None:
                        if m_arr1 is not None and m_dep2 < m_arr1:
                            day_off += 1
                        utc_abs2 = day_off * 1440 + m_dep2 - 480
                        utc_d2 = utc_abs2 % 1440
                        utc_a2 = (m_arr2 - 480) % 1440
                        d_shift2 = utc_abs2 // 1440
                        utc_days2 = {(day + d_shift2) % 7 for day in bjt_days}
                        legs.append({
                            "op": op, "cs": cs, "num": num, "orig": d1, "dest": d2_ap,
                            "leg_no": 2, "is_multileg": True,
                            "bjt_days": f"{d.get('FD3')}(+{day_off}d)" if day_off else str(d.get("FD3") or ""),
                            "bjt_dep": fmt_hm_min(m_dep2), "bjt_arr": fmt_hm_min(m_arr2),
                            "utc_dep": fmt_hm_min(utc_d2), "utc_arr": fmt_hm_min(utc_a2),
                            "utc_dep_min": utc_d2, "utc_arr_min": utc_a2,
                            "utc_days": utc_days2, "fd2": fd2, "fd19": fd19, "fd26": fd26,
                            "fd23": fd23, "fd24": fd24, "expired": expired, "grp": grp,
                            "route_type": "cargo" if (op == "CAO" or fd26 == "F") else "scheduled",
                            "src_ref": f"{fn}:{r_i}#2",
                        })
    return raw_rows, legs


def detect_rotation_gaps(legs):
    """CAAC pre-plan lists only legs touching China. When outbound N ends at X but return N+1 starts at Y (X != Y),
    the foreign-to-foreign leg X->Y (flown under N's callsign, e.g. CAO3155 ESSA->EBLG, CAO1011 PANC->KORD) is
    absent from the plan. Only DETECT it here; the leg itself is built solely from real FR24 observations."""
    by_num = defaultdict(list)
    for l in legs:
        if not l["expired"]:
            by_num[l["num"]].append(l)
    gaps = {}
    for num, out_legs in sorted(by_num.items()):
        m = re.fullmatch(r"CA(\d+)", num)
        if not m or int(m.group(1)) % 2 == 0:
            continue
        ret_legs = by_num.get(f"CA{int(m.group(1)) + 1}")
        if not ret_legs:
            continue
        out_end = {l["dest"] for l in out_legs} - {l["orig"] for l in out_legs}
        ret_start = {l["orig"] for l in ret_legs} - {l["dest"] for l in ret_legs}
        if len(out_end) != 1 or len(ret_start) != 1 or out_end == ret_start:
            continue
        x, y = next(iter(out_end)), next(iter(ret_start))
        out = out_legs[0]
        gaps[(out["op"], out["cs"], x, y)] = {
            "op": out["op"], "out_cs": out["cs"], "ret_cs": ret_legs[0]["cs"], "orig": x, "dest": y,
            "route_type": out["route_type"], "status": "", "acts": 0, "obs": 0, "last_act": "", "v_ids": "", "detail": "",
        }
    return gaps


def load_vamsys_db():
    conn = sqlite3.connect(f"file:{WORKSPACES_DB}?mode=ro", uri=True)
    cur = conn.cursor()
    rev = cur.execute("SELECT revision FROM workspace_revisions WHERE workspace_id=?", (VAMSYS_WS_ID,)).fetchone()[0]
    airports = {
        json.loads(j)["Fields"]["ICAO/IATA"].strip().upper(): json.loads(j)["Fields"]
        for (j,) in cur.execute("SELECT json FROM resource_rows WHERE workspace_id=? AND resource=0 AND collection=1", (VAMSYS_WS_ID,))
    }
    fleets = {
        json.loads(j)["Fields"]["ID"]: json.loads(j)["Fields"]
        for (j,) in cur.execute("SELECT json FROM resource_rows WHERE workspace_id=? AND resource=1 AND collection=1", (VAMSYS_WS_ID,))
    }
    aircraft = [
        json.loads(j)["Fields"]
        for (j,) in cur.execute("SELECT json FROM resource_rows WHERE workspace_id=? AND resource=2 AND collection=1", (VAMSYS_WS_ID,))
    ]
    routes = []
    for (j,) in cur.execute("SELECT json FROM resource_rows WHERE workspace_id=? AND resource=4 AND collection=1", (VAMSYS_WS_ID,)):
        row = json.loads(j)
        f = row["Fields"]
        raw = json.loads(row.get("RawApiJson") or "{}")
        cs = f["Callsign"].strip().upper()
        op = cs[:3] if len(cs) >= 3 and cs[:3].isalpha() else cs
        f["_raw"] = raw
        f["_op"] = op
        f["_cs"] = cs
        f["_fn"] = f["Flight Number"].strip().upper()
        f["_orig"] = f["Departure Airport (ICAO/IATA)"].strip().upper()
        f["_dest"] = f["Arrival Airport (ICAO/IATA)"].strip().upper()
        f["_dep_min"] = parse_hhmm(f.get("Departure Time (HH:MM)"))
        f["_arr_min"] = parse_hhmm(f.get("Arrival Time (HH:MM)"))
        f["_dep_hm"] = fmt_hm_min(f["_dep_min"]) if f["_dep_min"] is not None else ""
        f["_arr_hm"] = fmt_hm_min(f["_arr_min"]) if f["_arr_min"] is not None else ""
        f["_days"] = vdays_to_day_set(f.get("Service Days"))
        f["_fids"] = {x.strip() for x in (f.get("Fleet IDs") or "").split(",") if x.strip()}
        f["_is_special"] = bool(
            f.get("Type") in ("training", "repositioning")
            or op not in TARGET_OPS
            or any(t in (f.get("Tags") or "") for t in ("MIL", "TRAINING"))
        )
        routes.append(f)
    conn.close()
    return rev, airports, fleets, aircraft, routes


def load_fr24_unified(cao_flight_nums):
    live = json.load(open(f"{CACHE_DIR}/live_summary.json", encoding="utf-8"))
    search = json.load(open(f"{CACHE_DIR}/search_index.json", encoding="utf-8"))
    fh = json.load(open(f"{CACHE_DIR}/flight_histories.json", encoding="utf-8")).get("flights", {})
    reg_path = os.path.join(CACHE_DIR, "reg_histories.json")
    reg_hist = json.load(open(reg_path, encoding="utf-8")).get("flights", {}) if os.path.exists(reg_path) else {}
    ap = {fn[:-5]: json.load(open(os.path.join(AP_CACHE_DIR, fn), encoding="utf-8"))
          for fn in os.listdir(AP_CACHE_DIR) if fn.endswith(".json")}

    by_flight_search = search.get("by_flight", {})
    by_cs_search = search.get("by_callsign", {})
    iata_to_icao = {}

    for d in live.get("details", {}).values():
        if d.get("origin_iata") and d.get("origin_icao"):
            iata_to_icao.setdefault(d["origin_iata"], d["origin_icao"])
        if d.get("dest_iata") and d.get("dest_icao"):
            iata_to_icao.setdefault(d["dest_iata"], d["dest_icao"])

    def norm_ap(icao, iata):
        code = None
        if icao:
            code = str(icao).upper().strip()
        elif iata:
            code = iata_to_icao.get(str(iata).upper().strip(), str(iata).upper().strip())
        return FR24_TO_VAMSYS_AP.get(code, code) if code else None

    def s_op_lookup(num):
        return (((by_flight_search.get(num) or {}).get("detail") or {}).get("operator") or "").upper()

    def is_cao_ph(cs, num, ac_code, is_act):
        if is_act:
            return False
        is_c = (ac_code in CARGO_AC_CODES) or (num in cao_flight_nums) or (s_op_lookup(num) == "CAO")
        return is_c and (not cs or cs.startswith("CCA"))

    fn_auth = {}
    for op, fls in live.get("live", {}).items():
        for f in fls:
            cs, num = (f.get("callsign") or "").upper(), (f.get("number") or "").upper()
            al_op = (f.get("airline_icao") or op).upper()
            if num and cs and al_op in TARGET_OPS:
                fn_auth[num] = (al_op, cs)
    for d in live.get("details", {}).values():
        cs, num = (d.get("callsign") or "").upper(), (d.get("number_default") or "").upper()
        al_op = (d.get("airline_icao") or d.get("operator_query") or "").upper()
        if num and cs and al_op in TARGET_OPS:
            fn_auth[num] = (al_op, cs)

    for actual_pass in (True, False):
        for payload in ap.values():
            for r in payload.get("rows", []):
                if r.get("origin_iata") and r.get("origin_icao"):
                    iata_to_icao.setdefault(r["origin_iata"], r["origin_icao"])
                if r.get("dest_iata") and r.get("dest_icao"):
                    iata_to_icao.setdefault(r["dest_iata"], r["dest_icao"])
                is_act = row_is_actual(r)
                if is_act != actual_pass:
                    continue
                cs, num, ac = (r.get("callsign") or "").upper(), (r.get("number") or "").upper(), (r.get("aircraft_code") or "").upper()
                if not num:
                    continue
                if is_cao_ph(cs, num, ac, is_act):
                    fn_auth.setdefault(num, ("CAO", ("CAO" + num[2:]) if num.startswith("CA") else num))
                elif cs.startswith(TARGET_OPS):
                    fn_auth.setdefault(num, (cs[:3], cs))

    for actual_pass in (True, False):
        for fl_num, payload in fh.items():
            for r in payload.get("rows", []):
                if str(r.get("status_generic") or "").lower() in ("canceled", "diverted"):
                    continue
                ev_ts = r.get("real_dep") or r.get("sched_dep") or r.get("real_arr") or r.get("sched_arr")
                if not ev_ts or int(ev_ts) < WINTER_START_TS:
                    continue
                is_act = row_is_actual(r)
                if is_act != actual_pass:
                    continue
                raw_al = (r.get("airline_icao") or "").upper()
                cs, num, ac = (r.get("callsign") or "").upper(), (r.get("number") or fl_num or "").upper(), (r.get("aircraft_code") or "").upper()
                if not num or (raw_al and raw_al not in TARGET_OPS):
                    continue
                if cs and len(cs) >= 3 and cs[:3].isalpha() and not cs.startswith(TARGET_OPS):
                    continue
                if is_cao_ph(cs, num, ac, is_act):
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

    def classify(r, default_num):
        raw_al = (r.get("airline_icao") or "").upper()
        cs = (r.get("callsign") or "").upper()
        num = (r.get("number") or default_num or "").upper()
        alt = (r.get("number_alt") or "").upper()
        ac = (r.get("aircraft_code") or "").upper()
        if raw_al and raw_al not in TARGET_OPS:
            return None
        if cs and len(cs) >= 3 and cs[:3].isalpha() and not cs.startswith(TARGET_OPS):
            return None
        is_act = row_is_actual(r)
        s_op = s_op_lookup(num)
        ph = is_cao_ph(cs, num, ac, is_act)
        src = "airport_callsign"
        if cs.startswith(TARGET_OPS) and not ph:
            op = cs[:3]
        elif alt.startswith(TARGET_OPS) and not (not is_act and alt.startswith("CCA") and (ac in CARGO_AC_CODES or num in cao_flight_nums or s_op == "CAO")):
            op, cs = alt[:3], alt
        elif ph or ac in CARGO_AC_CODES or num in cao_flight_nums or s_op == "CAO":
            op, src = "CAO", "cargo_fallback"
            cs = ("CAO" + num[2:]) if num.startswith("CA") else (num or cs)
        elif num in fn_auth:
            op, cs = fn_auth[num]
            src = "search_resolved"
        elif raw_al in TARGET_OPS:
            op, src = raw_al, "airport_iata_fallback"
            cs = (op + num[2:]) if num.startswith("CA") else (num or "")
        else:
            return None
        return op, cs, num, src, is_act

    obs = []
    seen = set()
    seen_ids = set()
    details_map = live.get("details", {})

    for op, fls in live.get("live", {}).items():
        for f in fls:
            fid = f["id"]
            d = details_map.get(fid)
            if d:
                al_op = (d.get("airline_icao") or op).upper()
                cs, num = (d.get("callsign") or "").upper(), (d.get("number_default") or "").upper()
                orig, dest = norm_ap(d.get("origin_icao"), d.get("origin_iata")), norm_ap(d.get("dest_icao"), d.get("dest_iata"))
                tm = d.get("time") or {}
                s_dep, s_arr = (tm.get("scheduled") or {}).get("departure"), (tm.get("scheduled") or {}).get("arrival")
                r_dep, r_arr = (tm.get("real") or {}).get("departure"), (tm.get("real") or {}).get("arrival")
                ac = d.get("aircraft_code")
            else:
                al_op = (f.get("airline_icao") or op).upper()
                cs, num = (f.get("callsign") or "").upper(), (f.get("number") or "").upper()
                orig, dest = norm_ap(None, f.get("origin_iata")), norm_ap(None, f.get("destination_iata"))
                s_dep = s_arr = r_dep = r_arr = None
                ac = f.get("aircraft_code")
            if orig and dest and ("live", fid) not in seen:
                seen.add(("live", fid))
                if fid:
                    seen_ids.add(fid)
                ev_ts = r_dep or s_dep or r_arr or s_arr or f.get("time") or NOV_START_TS
                obs.append({
                    "source": "live", "op": al_op, "op_source": "live",
                    "callsign": cs or (al_op + num[2:] if num.startswith("CA") else num),
                    "number": num, "orig": orig, "dest": dest, "ac": (ac or "").upper(),
                    "sched_dep": s_dep, "sched_arr": s_arr, "real_dep": r_dep, "real_arr": r_arr,
                    "is_actual": True, "ev_ts": int(ev_ts), "is_live_or_fut_sched": True,
                })

    for payload in ap.values():
        for r in payload.get("rows", []):
            res = classify(r, None)
            if not res:
                continue
            op, cs, num, src, is_act = res
            orig, dest = norm_ap(r.get("origin_icao"), r.get("origin_iata")), norm_ap(r.get("dest_icao"), r.get("dest_iata"))
            if not orig or not dest:
                continue
            fid = r.get("id")
            dk = ("ap_id", fid) if fid else ("ap_sched", cs or num, orig, dest, r.get("sched_dep"))
            if dk not in seen:
                seen.add(dk)
                if fid:
                    seen_ids.add(fid)
                ev_ts = r.get("real_dep") or r.get("sched_dep") or r.get("real_arr") or r.get("sched_arr")
                obs.append({
                    "source": "airport", "op": op, "op_source": src, "callsign": cs, "number": num,
                    "orig": orig, "dest": dest, "ac": (r.get("aircraft_code") or "").upper(),
                    "sched_dep": r.get("sched_dep"), "sched_arr": r.get("sched_arr"),
                    "real_dep": r.get("real_dep"), "real_arr": r.get("real_arr"),
                    "is_actual": is_act, "ev_ts": int(ev_ts) if ev_ts else None, "is_live_or_fut_sched": True,
                })

    pre_by_op_cs_od = defaultdict(list)
    pre_by_num_od = defaultdict(list)
    pre_by_cs = defaultdict(list)
    # Flight-number histories + aircraft-registration histories (continuation legs such as CAO3155 ESSA->EBLG are
    # published by FR24 with a callsign but NO flight number, so only the registration query returns them).
    gold_sources = [(fl_num, fl_num, payload) for fl_num, payload in fh.items()]
    gold_sources += [(f"reg:{reg}", None, payload) for reg, payload in reg_hist.items()]
    pre_seen_ids = set()
    for fl_num, default_num, payload in gold_sources:
        for r in payload.get("rows", []):
            st_gen = str(r.get("status_generic") or "").lower()
            if st_gen in ("canceled", "diverted"):
                continue
            res = classify(r, default_num)
            if not res:
                continue
            op, cs, num, src, is_act = res
            orig, dest = norm_ap(r.get("origin_icao"), r.get("origin_iata")), norm_ap(r.get("dest_icao"), r.get("dest_iata"))
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
                dk = ("ap_sched", cs or num, orig, dest, r.get("sched_dep"))
                if not fid and dk in seen:
                    continue
                if fid:
                    seen_ids.add(fid)
                seen.add(dk)
                obs.append({
                    "source": f"gold:{fl_num}", "op": op, "op_source": src, "callsign": cs, "number": num,
                    "orig": orig, "dest": dest, "ac": (r.get("aircraft_code") or "").upper(),
                    "sched_dep": r.get("sched_dep"), "sched_arr": r.get("sched_arr"),
                    "real_dep": r.get("real_dep"), "real_arr": r.get("real_arr"),
                    "is_actual": is_act, "ev_ts": ev_ts,
                    "is_live_or_fut_sched": bool(is_act or st_gen == "scheduled" or ev_ts >= ACTIVE_SCHED_CUTOFF_TS),
                })
            else:
                fid = r.get("id")
                if fid and fid in pre_seen_ids:
                    continue
                if fid:
                    pre_seen_ids.add(fid)
                rec = {
                    "op": op, "cs": cs, "num": num, "op_source": src,
                    "orig": orig, "dest": dest, "ts": ev_ts, "is_actual": is_act, "ac": (r.get("aircraft_code") or "").upper(),
                }
                pre_by_op_cs_od[(op, cs, orig, dest)].append(rec)
                if num:
                    pre_by_num_od[(num, orig, dest)].append(rec)
                if cs:
                    pre_by_cs[cs].append(rec)

    by_op_cs_od = defaultdict(list)
    by_num_od = defaultdict(list)
    by_cs = defaultdict(list)
    for o in obs:
        by_op_cs_od[(o["op"], o["callsign"], o["orig"], o["dest"])].append(o)
        if o["number"]:
            by_num_od[(o["number"], o["orig"], o["dest"])].append(o)
        if o["callsign"]:
            by_cs[o["callsign"]].append(o)

    def probe(op, cs, orig, dest):
        num = ("CA" + cs[3:]) if cs.startswith(TARGET_OPS) else cs
        cands = list(by_op_cs_od.get((op, cs, orig, dest), []))
        if cands and all(o["op_source"] == "airport_iata_fallback" for o in cands):
            if num in fn_auth and fn_auth[num][0] != op:
                cands = []
        if cands:
            acts = [o for o in cands if o["is_actual"]]
            active_sched = [o for o in cands if o.get("is_live_or_fut_sched")]
            if not acts and not active_sched:
                stale_ts = [o["ev_ts"] for o in cands if o.get("ev_ts")]
                return "stale_sched_only", cands, {"max_stale": fmt_date_ts(max(stale_ts)) if stale_ts else "", "stale_n": len(cands)}
            return ("direct_actual" if acts else "direct_sched"), cands, {"acts": len(acts)}

        same_num_od = by_num_od.get((num, orig, dest), [])
        other_op = [o for o in same_num_od if o["op"] != op and o["op_source"] != "airport_iata_fallback"]
        if other_op:
            op_cnts = Counter((o["op"], o["callsign"]) for o in other_op if o["is_actual"]) or Counter((o["op"], o["callsign"]) for o in other_op)
            return "op_mismatch", [], {"other_ops": op_cnts.most_common()}

        other_rt = by_cs.get(cs, [])
        if other_rt:
            rt_cnts = Counter((o["orig"], o["dest"]) for o in other_rt if o["is_actual"]) or Counter((o["orig"], o["dest"]) for o in other_rt)
            return "route_mismatch", [], {"other_ods": rt_cnts.most_common()}

        exact_pre = [x for x in pre_by_op_cs_od.get((op, cs, orig, dest), []) if x["is_actual"]]
        if exact_pre:
            return "pre_winter_stopped", [], {"last_date": fmt_date_ts(max(x["ts"] for x in exact_pre)), "n_pre": len(exact_pre)}
        other_pre_op = [x for x in pre_by_num_od.get((num, orig, dest), [])
                        if x["op"] != op and x["op_source"] != "airport_iata_fallback" and x["is_actual"]]
        if other_pre_op:
            return "pre_winter_op_mismatch", [], {"last_date": fmt_date_ts(max(x["ts"] for x in other_pre_op)), "pre_ops": sorted({(x["op"], x["cs"]) for x in other_pre_op})}
        other_pre_rt = [x for x in pre_by_cs.get(cs, []) if x["is_actual"]]
        if other_pre_rt:
            return "pre_winter_other_route", [], {"last_date": fmt_date_ts(max(x["ts"] for x in other_pre_rt)), "pre_ods": sorted({(x["orig"], x["dest"]) for x in other_pre_rt})}
        if num in fh:
            return "zero_in_365d", [], {"cached_rows": len(fh[num].get("rows", []))}
        s_item = by_cs_search.get(cs) or by_flight_search.get(num)
        if s_item:
            return "search_only", [], {"detail": s_item.get("detail")}
        return "not_found", [], {}

    return {
        "obs": obs, "fh": fh, "reg_hist": reg_hist, "by_op_cs_od": by_op_cs_od, "by_num_od": by_num_od, "by_cs": by_cs,
        "probe": probe, "ap_count": len(ap), "fh_count": len(fh), "search_count": len(by_flight_search),
    }


def _extract_fr24_pair_rows(cands):
    """Return list of (dep_hm, arr_hm, wd_int, is_act, is_nov, is_real_fb) from scheduled or real FR24 rows."""
    sched_rows = [o for o in cands if o.get("sched_dep") and o.get("sched_arr")]
    is_real_fb = False
    if not sched_rows:
        sched_rows = [o for o in cands if o.get("real_dep") and o.get("real_arr")]
        is_real_fb = True
    out = []
    for o in sched_rows:
        dep_ts = o.get("sched_dep") if not is_real_fb else ((int(o["real_dep"]) + 150) // 300) * 300
        arr_ts = o.get("sched_arr") if not is_real_fb else ((int(o["real_arr"]) + 150) // 300) * 300
        dt_d = datetime.datetime.fromtimestamp(int(dep_ts), tz=datetime.timezone.utc)
        dt_a = datetime.datetime.fromtimestamp(int(arr_ts), tz=datetime.timezone.utc)
        out.append((
            dt_d.strftime("%H:%M"), dt_a.strftime("%H:%M"),
            dt_d.weekday(), bool(o.get("is_actual")), (o.get("ev_ts") or 0) >= NOV_START_TS,
            is_real_fb, int(dep_ts),
        ))
    return out


def extract_fr24_slots_and_fleets(op, cands):
    """Return (fr_pairs_summary, fr_active_fleet_ids, fr_ac_counter, has_nov_plus, last_act_date, fr_days_set)."""
    if not cands:
        return [], set(), Counter(), False, "", set()
    acts = [o for o in cands if o["is_actual"]]
    ac_cnt = Counter(o["ac"] for o in (acts or cands) if o.get("ac"))
    parsed = _extract_fr24_pair_rows(cands)
    nov_rows = [r for r in parsed if r[4]]
    eval_rows = nov_rows or parsed
    pair_cnt = Counter()
    days_by_pair = defaultdict(set)
    all_days = set()
    for d_hm, a_hm, wd, *_ in eval_rows:
        pair = (d_hm, a_hm)
        pair_cnt[pair] += 1
        days_by_pair[pair].add(wd)
        all_days.add(wd)

    fr_fids = set()
    for ac_code in ac_cnt:
        fid = map_ac_to_fleet(op, ac_code)
        if fid:
            fr_fids.add(fid)

    last_ts = max((o["ev_ts"] for o in acts if o.get("ev_ts")), default=None)
    fr_pairs = [(d, a, cnt, day_set_to_fd3(days_by_pair[(d, a)])) for (d, a), cnt in pair_cnt.most_common()]
    return fr_pairs, fr_fids, ac_cnt, bool(nov_rows), fmt_date_ts(last_ts) if last_ts else "", all_days


def cluster_fr24_slots(cands):
    """Cluster FR24 Winter scheduled (or fallback real) times into distinct operational banks."""
    parsed = _extract_fr24_pair_rows(cands)
    if not parsed:
        return []

    pair_score = Counter()
    for d_hm, a_hm, wd, is_act, is_nov, *_ in parsed:
        w = (3 if is_act else 1) * (4 if is_nov else 1)
        pair_score[(d_hm, a_hm)] += w

    clusters = []
    for (d_hm, a_hm), w in pair_score.most_common():
        placed = False
        for cl in clusters:
            dd = abs(hm_circ_diff(d_hm, cl["mode_dep"]))
            da = abs(hm_circ_diff(a_hm, cl["mode_arr"]))
            if (dd <= 45 and da <= 60) or (dd <= 20 and da <= 75) or (dd <= 65 and da <= 20):
                cl["weights"][(d_hm, a_hm)] = w
                placed = True
                break
        if not placed:
            clusters.append({
                "mode_dep": d_hm, "mode_arr": a_hm,
                "weights": {(d_hm, a_hm): w},
                "days": set(), "act_days": set(), "nov_days": set(),
                "day_acts": Counter(), "nov_wd_obs": Counter(),
                "acts": 0, "obs": 0, "nov_obs": 0, "first_ts": None, "last_ts": None,
            })

    for d_hm, a_hm, wd, is_act, is_nov, _, dep_ts in parsed:
        best_cl = min(clusters, key=lambda c: abs(hm_circ_diff(d_hm, c["mode_dep"])) + abs(hm_circ_diff(a_hm, c["mode_arr"])))
        best_cl["obs"] += 1
        best_cl["first_ts"] = dep_ts if best_cl["first_ts"] is None else min(best_cl["first_ts"], dep_ts)
        best_cl["last_ts"] = dep_ts if best_cl["last_ts"] is None else max(best_cl["last_ts"], dep_ts)
        best_cl.setdefault("ts_list", []).append(dep_ts)
        best_cl["days"].add(wd)
        if is_act:
            best_cl["acts"] += 1
            best_cl["act_days"].add(wd)
            best_cl["day_acts"][wd] += 1
        if is_nov:
            best_cl["nov_obs"] += 1
            best_cl["nov_days"].add(wd)
            best_cl["nov_wd_obs"][wd] += 1

    clusters = [c for c in clusters if c["obs"]]
    clusters = drop_superseded_banks(clusters)
    clusters.sort(key=lambda c: (c["acts"], c["nov_obs"], c["obs"]), reverse=True)
    return clusters


SUPERSEDE_GAP_S = 7 * 86400


def drop_superseded_banks(clusters):
    """Drop banks replaced by a mid-season timetable change (e.g. CAO3155 18:00Z until 11-03, then 20:15Z from 11-05):
    a bank is superseded when its last sample is >7 days older than the latest bank's last sample and the latest
    bank has >=2 samples after it (FR24 may briefly carry both timings, so overlap at season start is allowed)."""
    if len(clusters) < 2:
        return clusters
    latest = max(clusters, key=lambda c: c["last_ts"])
    kept = []
    for c in clusters:
        n_after = sum(1 for t in latest.get("ts_list", []) if t > c["last_ts"])
        if c is not latest and c["last_ts"] < latest["last_ts"] - SUPERSEDE_GAP_S and n_after >= 2:
            latest.setdefault("superseded", []).append(f"{c['mode_dep']}-{c['mode_arr']}(末次{fmt_date_ts(c['last_ts'])})")
            continue
        kept.append(c)
    return kept


def eval_vamsys_fleets(op, orig, dest, vr, v_fleets, rec_fids, body_tier, route_grade, grade_note=""):
    """Compare existing VAMSYS fleets strictly against Body-Tier + Flight-Zone-Grade targets (ignoring active/retired status)."""
    v_fset = vr["_fids"]
    v_clean = v_fset - EXCLUDED_DIRTY_FLEETS - AIR_FORCE_FLEETS
    fleet_issues = []

    if "26728" in v_fset:
        fleet_issues.append("误含异常机队DH8D(26728)")
    if v_fset & AIR_FORCE_FLEETS:
        fleet_issues.append(f"误含空军机型({fleet_codes_str(v_fset & AIR_FORCE_FLEETS, v_fleets)})")

    miss = rec_fids - v_clean
    extra = v_clean - rec_fids

    if op in ("CAO", "CCD", "BJN"):
        if miss:
            fleet_issues.append(f"缺{op}机队({fleet_codes_str(miss, v_fleets)})")
        if extra:
            fleet_issues.append(f"含非{op}允许机型({fleet_codes_str(extra, v_fleets)})")
    else:
        if grade_note and ("降级" in grade_note or "仅配B762/B763" in grade_note):
            fleet_issues.append(grade_note)
        if extra:
            fleet_issues.append(f"含超飞行区等级或跨宽窄机型({fleet_codes_str(extra, v_fleets)})")
        if miss:
            fleet_issues.append(f"按[{body_tier}+{route_grade}]需补齐({fleet_codes_str(miss, v_fleets)})")

    rec_out = set(rec_fids)
    return fleet_issues, "", rec_out


def compute_recommended_fleets(op, orig, dest, leg_rows, fr_fids, route_type, v_cands=None):
    """Step 1: Determine Body Tier (NB/WB/MIX) from Plan FD2 + FR24 (NOT FD19);
    Step 2: Match ALL VAMSYS commercial Fleet IDs (ignoring active/retired, excl. dirty DH8D) by O/D Flight Zone Grade.
    Returns (rec_fids, body_tier, grade_label, grade_note)."""
    g_orig, g_dest, g_min = route_min_grade(orig, dest)
    grade_label = f"{g_orig}->{g_dest}(取{g_min})"
    grade_note = ""

    if op == "CAO" or route_type == "cargo":
        if g_min in ("3C", "4C", "4D"):
            grade_note = f"注意:货机飞行区等级{grade_label}低于4E"
        return set(CAO_ACTIVE), "全货机(CAO)", grade_label, grade_note
    if op == "BJN":
        if g_min == "3C":
            grade_note = f"注意:BJN 738需>=4C，{grade_label}"
        return set(BJN_ACTIVE), "窄体(BJN)", grade_label, grade_note
    if op == "CCD":
        if g_min == "3C":
            grade_note = f"注意:CCD 738需>=4C，{grade_label}"
        return set(CCD_ACTIVE), "窄体(CCD)", grade_label, grade_note

    # Step 1: CCA Passenger Body Tier from Plan FD2 + FR24 actual/sched fleets (ignoring boilerplate FD19)
    fd2s = {str(l.get("fd2") or "").upper().strip() for l in leg_rows if l.get("fd2")}
    has_nb_fd2 = any(map_ac_to_fleet("CCA", c) in NB_SET for c in fd2s)
    has_wb_fd2 = any((map_ac_to_fleet("CCA", c) in WB_SET) or c in ("330", "777", "787", "747") for c in fd2s)
    has_nb_fr = bool(fr_fids & NB_SET)
    has_wb_fr = bool(fr_fids & WB_SET)

    has_nb = has_nb_fd2 or has_nb_fr
    has_wb = has_wb_fd2 or has_wb_fr

    # Fallback only when neither Plan FD2 nor FR24 provides an aircraft code
    if not has_nb and not has_wb and v_cands:
        v_union = set().union(*(vr["_fids"] for vr in v_cands)) - EXCLUDED_DIRTY_FLEETS - AIR_FORCE_FLEETS
        has_nb = bool(v_union & NB_SET)
        has_wb = bool(v_union & WB_SET)
    if not has_nb and not has_wb:
        has_nb = True
        grade_note = "缺计划FD2与FR24机型，暂按窄体"

    if has_nb and has_wb:
        body_tier = "宽窄混飞"
    elif has_wb:
        body_tier = "宽体"
    else:
        body_tier = "窄体"

    # Step 2: Match ALL Param 9671 Fleet IDs by O/D flight zone grade + historic B757 high-plateau + stand rules
    if g_min == "3C":
        nb_pool = set(CCA_NB_3C)
    elif route_allows_b752(orig, dest):
        nb_pool = set(CCA_NB_4D)
    else:
        nb_pool = set(CCA_NB_4C)

    if g_min == "4F":
        wb_pool = set(CCA_WB_4F)
    elif g_min == "4E":
        wb_pool = set(CCA_WB_4E)
    elif g_min == "4D":
        wb_pool = set(CCA_WB_4D)
    else:
        wb_pool = set()

    if body_tier == "窄体":
        rec_fids = nb_pool
    elif body_tier == "宽体":
        if wb_pool:
            rec_fids = wb_pool
            if g_min == "4D":
                grade_note = f"飞行区等级{grade_label}为4D(宽体仅配B762/B763)"
        else:
            rec_fids = nb_pool
            grade_note = f"飞行区等级{grade_label}低于4D(宽体受限，已降级窄体池)"
    else:
        if wb_pool:
            rec_fids = nb_pool | wb_pool
            if g_min == "4D":
                grade_note = f"飞行区等级{grade_label}为4D(混飞宽体仅配B762/B763)"
        else:
            rec_fids = nb_pool
            grade_note = f"飞行区等级{grade_label}低于4D(混飞仅保留窄体池)"

    excl = get_route_fleet_exclusions(orig, dest, route_type)
    if excl:
        rec_fids = rec_fids - excl
        if not rec_fids:
            rec_fids = nb_pool - excl

    return rec_fids, body_tier, grade_label, grade_note


def detect_mixed_tz_and_choose_utc(leg, fr_pairs):
    """Return (rec_dep, rec_arr, tz_note, best_fr_diff_min)."""
    bjt_dur = (parse_hhmm(leg["bjt_arr"]) - parse_hhmm(leg["bjt_dep"])) % 1440
    ud, ua = leg["utc_dep"], leg["utc_arr"]
    if not fr_pairs:
        return ud, ua, "无FR24时刻样本(按BJT-8h)", None

    best_pair = min(fr_pairs, key=lambda p: abs(hm_circ_diff(ud, p[0])) + abs(hm_circ_diff(ua, p[1])))
    fr_d, fr_a = best_pair[0], best_pair[1]
    fr_dur = (parse_hhmm(fr_a) - parse_hhmm(fr_d)) % 1440
    dep_diff = hm_circ_diff(ud, fr_d)
    arr_diff = hm_circ_diff(ua, fr_a)

    dep_as_utc_hm = leg["bjt_dep"]
    if abs(bjt_dur - fr_dur) > 120 and abs(hm_circ_diff(dep_as_utc_hm, fr_d)) <= 20 and abs(arr_diff) <= 20:
        return fr_d, ua, f"源表FD5疑似直接写UTC({dep_as_utc_hm})而FD6写BJT，已按FR24核准UTC", 0
    return ud, ua, f"FR24差(dep={dep_diff:+}m,arr={arr_diff:+}m)", dep_diff


def calibrate_slots_with_fr24(raw_slots, fr_cl, is_fr_confirmed, promoted_expired, v_cands):
    """Merge fake Pre-Plan weekday micro-splits and calibrate times/weekdays/banks with FR24 Reality."""
    # Step 1: Attach each raw plan slot to closest FR24 cluster (<=45m dep, <=60m arr) and merge micro-splits
    single_fr_bank = (is_fr_confirmed and len(fr_cl) == 1)
    grouped = []
    for sl in raw_slots:
        c_idx = None
        if is_fr_confirmed and fr_cl:
            best_i = min(range(len(fr_cl)), key=lambda idx: abs(hm_circ_diff(sl["dep"], fr_cl[idx]["mode_dep"])) + abs(hm_circ_diff(sl["arr"], fr_cl[idx]["mode_arr"])))
            if abs(hm_circ_diff(sl["dep"], fr_cl[best_i]["mode_dep"])) <= 45 and abs(hm_circ_diff(sl["arr"], fr_cl[best_i]["mode_arr"])) <= 60:
                c_idx = best_i

        merged = False
        for g in grouped:
            same_cl = (c_idx is not None and g["c_idx"] == c_idx)
            close_single = (single_fr_bank and abs(hm_circ_diff(sl["dep"], g["dep"])) <= 40 and abs(hm_circ_diff(sl["arr"], g["arr"])) <= 50)
            if same_cl or close_single:
                g["days"] |= sl["days"]
                g["subslots"].append((sl["dep"], sl["arr"], sl["fd3_utc"]))
                if c_idx is not None and g["c_idx"] is None:
                    g["c_idx"] = c_idx
                merged = True
                break
        if not merged:
            grouped.append({
                "dep": sl["dep"], "arr": sl["arr"], "days": set(sl["days"]),
                "plan_dep": sl["dep"], "plan_arr": sl["arr"], "plan_fd3": sl["fd3_utc"],
                "bjt": sl["bjt"], "tz_note": sl["tz_note"], "fd2": sl["fd2"], "fd19": sl["fd19"],
                "c_idx": c_idx, "subslots": [(sl["dep"], sl["arr"], sl["fd3_utc"])],
                "from_fr24_extra": False, "fr_cal_note": "", "fr_cl_ref": None,
            })

    # Aggregate FR24 weekday stats across all clusters
    total_acts = sum(c["acts"] for c in fr_cl)
    total_day_acts = Counter()
    total_nov_wd_obs = Counter()
    total_act_days = set()
    for c in fr_cl:
        total_day_acts += c["day_acts"]
        total_nov_wd_obs += c["nov_wd_obs"]
        total_act_days |= c["act_days"]

    used_fr_indices = set()
    for g in grouped:
        g["plan_fd3"] = day_set_to_fd3(g["days"])
        cl = None
        if is_fr_confirmed and fr_cl:
            if g["c_idx"] is not None:
                cl = fr_cl[g["c_idx"]]
                used_fr_indices.add(g["c_idx"])
            else:
                best_i = min(range(len(fr_cl)), key=lambda idx: abs(hm_circ_diff(g["dep"], fr_cl[idx]["mode_dep"])) + abs(hm_circ_diff(g["arr"], fr_cl[idx]["mode_arr"])))
                cand_cl = fr_cl[best_i]
                dd = abs(hm_circ_diff(g["dep"], cand_cl["mode_dep"]))
                da = abs(hm_circ_diff(g["arr"], cand_cl["mode_arr"]))
                # Calibrate DST 60m shift or single-slot wrong Plan time (e.g. CCA1215 7h off / BJN8603 30m off)
                if (dd <= 70 and da <= 85) or (len(grouped) == 1 and cand_cl["acts"] >= 2):
                    cl = cand_cl
                    used_fr_indices.add(best_i)

        cal_notes = []
        if len(g["subslots"]) > 1:
            cal_notes.append(f"已合并计划微拆班({len(g['subslots'])}子段->1班次)")

        if cl is not None:
            g["fr_cl_ref"] = cl
            if cl.get("superseded"):
                cal_notes.append(f"FR24换季中调时，旧时段{','.join(cl['superseded'])}已停用不另建")
            dd = abs(hm_circ_diff(g["plan_dep"], cl["mode_dep"]))
            da = abs(hm_circ_diff(g["plan_arr"], cl["mode_arr"]))
            trust_fr_time = bool(cl["acts"] >= 2 or (cl["nov_obs"] >= 2 and dd in (55, 60, 65)))
            if trust_fr_time and (dd > 15 or da > 15):
                g["dep"] = cl["mode_dep"]
                g["arr"] = cl["mode_arr"]
                cal_notes.append(f"按FR24实飞校准时刻(原计划{g['plan_dep']}-{g['plan_arr']}差{max(dd,da)}m)")

        # Weekday calibration from FR24 ground truth
        if promoted_expired and total_act_days:
            g["days"] = set(total_act_days)
            cal_notes.append("1025单日过期按FR24实飞重算星期")
        elif is_fr_confirmed and len(grouped) == 1 and total_acts >= 14:
            rep_extra = {wd for wd, cnt in total_day_acts.items() if cnt >= 2} - g["days"]
            ghost_days = {wd for wd in g["days"] if total_nov_wd_obs[wd] == 0}
            if rep_extra and len(rep_extra) >= 2 and len(ghost_days) >= 2 and len(total_act_days) >= 3:
                g["days"] = set(total_act_days)
                cal_notes.append(f"按FR24实飞重算星期(原计划{g['plan_fd3']}疑错位)")
            elif rep_extra:
                g["days"] = g["days"] | rep_extra
                cal_notes.append(f"按FR24实飞增补星期({g['plan_fd3']}->{day_set_to_fd3(g['days'])})")

        g["fd3_utc"] = day_set_to_fd3(g["days"])
        g["vdays"] = day_set_to_vdays(g["days"])
        g["fr_cal_note"] = " | ".join(cal_notes)

    # Step 2: Check for distinct active FR24 banks (>45m apart) missing completely from Plan!
    if is_fr_confirmed and fr_cl:
        for idx, cl in enumerate(fr_cl):
            if idx in used_fr_indices:
                continue
            already_near = any(
                abs(hm_circ_diff(g["dep"], cl["mode_dep"])) <= 45 and abs(hm_circ_diff(g["arr"], cl["mode_arr"])) <= 60
                for g in grouped
            )
            if already_near:
                continue
            v_matches_cl = any(
                vr["_dep_min"] is not None
                and abs(hm_circ_diff(vr["_dep_hm"], cl["mode_dep"])) <= 35
                and abs(hm_circ_diff(vr["_arr_hm"], cl["mode_arr"])) <= 45
                for vr in (v_cands or [])
            )
            if cl["acts"] >= 2 or (v_matches_cl and cl["obs"] >= 1):
                use_days = cl["act_days"] or cl["days"]
                fd3_s = day_set_to_fd3(use_days)
                grouped.append({
                    "dep": cl["mode_dep"], "arr": cl["mode_arr"], "days": set(use_days),
                    "plan_dep": "", "plan_arr": "", "plan_fd3": "",
                    "fd3_utc": fd3_s, "vdays": day_set_to_vdays(use_days),
                    "bjt": "(计划表漏列该时段)", "tz_note": "FR24实飞额外时段",
                    "fd2": grouped[0]["fd2"] if grouped else "",
                    "fd19": grouped[0]["fd19"] if grouped else "",
                    "c_idx": idx, "subslots": [], "from_fr24_extra": True,
                    "fr_cal_note": f"FR24实飞发现计划漏列独立时段(acts={cl['acts']},obs={cl['obs']})",
                    "fr_cl_ref": cl,
                })

    return grouped


def main():
    raw_rows, legs = load_raw_preplan()
    n_raw_legs = len(legs)
    cao_flight_nums = {"CA" + r["FD1"].strip().upper()[3:] for r in raw_rows if r["FD1"].strip().upper().startswith("CAO")}

    ws_rev, v_airports, v_fleets, v_aircraft, v_routes = load_vamsys_db()
    for vr in v_routes:
        if vr["_op"] == "CAO":
            cao_flight_nums.add("CA" + vr["_cs"][3:])

    fr = load_fr24_unified(cao_flight_nums)
    probe = fr["probe"]

    # Detect foreign-to-foreign legs the CAAC pre-plan omits (outbound end != return start); filled only from FR24 reality
    rotation_gaps = detect_rotation_gaps(legs)

    # Check VAMSYS routes airport usage (handling aliases)
    v_by_key = defaultdict(list)
    special_v_routes = []
    comm_v_routes = []
    for vr in v_routes:
        if vr["_is_special"]:
            special_v_routes.append(vr)
            continue
        comm_v_routes.append(vr)
        v_by_key[(vr["_op"], vr["_cs"], vr["_orig"], vr["_dest"])].append(vr)

    # Group raw legs by (op, cs, orig, dest), separating 1025-1025 expired vs 1025-0327 regular
    reg_legs_by_key = defaultdict(list)
    exp_legs = []
    for l in legs:
        if l["expired"]:
            exp_legs.append(l)
        else:
            reg_legs_by_key[(l["op"], l["cs"], l["orig"], l["dest"])].append(l)

    # Check 1025-1025 expired legs with FR24: promote FR24 active ones missing regular rows to active plan!
    expired_audit = []
    for l in exp_legs:
        k = (l["op"], l["cs"], l["orig"], l["dest"])
        has_reg = k in reg_legs_by_key
        kind, cands, info = probe(l["op"], l["cs"], l["orig"], l["dest"])
        fr_pairs, fr_fids, ac_cnt, has_nov, last_act, _ = extract_fr24_slots_and_fleets(l["op"], cands)
        acts_n = sum(1 for o in cands if o["is_actual"])
        if not has_reg and acts_n >= 5:
            # FR24 proves high-frequency ongoing winter operations (CCA754/796/890/974)!
            l_prom = dict(l)
            l_prom["_promoted_from_expired"] = True
            reg_legs_by_key[k].append(l_prom)
            decision = "无常驻行但FR24高频实飞->纳入在飞保护/维护"
        elif has_reg:
            decision = "同OD另有1025-0327常驻行->忽略该单日过期行"
        else:
            decision = "FR24确认停飞(11月0飞)->忽略该过期行"
        expired_audit.append({
            "cs": l["cs"], "orig": l["orig"], "dest": l["dest"], "has_reg": has_reg,
            "kind": kind, "acts": acts_n, "last_act": last_act or info.get("last_date", ""),
            "decision": decision,
        })

    # Reconcile Carrier Overrides from FR24 (100% consistent winter carrier shift, e.g. CCA8395/8396 -> BJN)
    # Build canonical normalized plan groups keyed by resolved_key = (target_op, target_cs, target_orig, target_dest)
    plan_groups = {}
    carrier_fixes = []
    for (op, cs, orig, dest), lrows in sorted(reg_legs_by_key.items()):
        kind, cands, info = probe(op, cs, orig, dest)
        target_op, target_cs, carrier_note = op, cs, ""
        # Check if 100% shifted carrier in FR24 Winter (0 same-op winter obs & >0 other_op actuals)
        if kind == "op_mismatch":
            top_op, top_cs = info["other_ops"][0][0]
            target_op, target_cs = top_op, top_cs
            carrier_note = f"FR24换季100%由{top_cs}执飞(原表写{cs})"
            carrier_fixes.append((cs, orig, dest, top_cs, info["other_ops"]))
            # re-probe under target carrier to get actual observations & fleets
            kind, cands, info = probe(target_op, target_cs, orig, dest)
        elif kind == "direct_actual":
            # Check mixed wet-lease share (e.g. CCA1323/1813/1903 vs CCD)
            num = "CA" + target_cs[3:]
            same_num = fr["by_num_od"].get((num, orig, dest), [])
            other_acts = Counter(o["op"] for o in same_num if o["is_actual"] and o["op"] != target_op and o["op_source"] != "airport_iata_fallback")
            if other_acts:
                carrier_note = f"混飞: 主{target_op}({info['acts']}班)+他司{dict(other_acts)}"

        fr_pairs, fr_fids, ac_cnt, has_nov, last_act, fr_days = extract_fr24_slots_and_fleets(target_op, cands)
        fr_cl = cluster_fr24_slots(cands)
        target_orig = FR24_TO_VAMSYS_AP.get(orig, orig)
        target_dest = FR24_TO_VAMSYS_AP.get(dest, dest)
        ap_note = ""
        if target_orig not in v_airports or target_dest not in v_airports:
            miss = [a for a in (target_orig, target_dest) if a not in v_airports]
            ap_note = f"VAMSYS缺场 {','.join(miss)}"

        # Calibrate each plan leg's UTC time & merge identical (rec_dep, rec_arr) slots across days
        slot_days = defaultdict(set)
        slot_meta = {}
        tz_issues = []
        for l in lrows:
            rec_dep, rec_arr, tz_note, dep_diff = detect_mixed_tz_and_choose_utc(l, fr_pairs)
            if "FD5疑似直接写UTC" in tz_note:
                tz_issues.append(tz_note)
            if "FD5疑似直接写UTC" in tz_note:
                u_days = fd3_to_day_set(l["bjt_days"].split("(")[0])
            else:
                u_days = l["utc_days"]
            slot_key = (rec_dep, rec_arr)
            slot_days[slot_key] |= u_days
            slot_meta[slot_key] = (l, tz_note, dep_diff)

        raw_slots = []
        for (rd, ra), dset in sorted(slot_days.items()):
            l_ref, tz_note, dep_diff = slot_meta[(rd, ra)]
            raw_slots.append({
                "dep": rd, "arr": ra, "days": dset,
                "fd3_utc": day_set_to_fd3(dset), "vdays": day_set_to_vdays(dset),
                "bjt": f"{l_ref['bjt_days']} {l_ref['bjt_dep']}-{l_ref['bjt_arr']}",
                "tz_note": tz_note, "dep_diff": dep_diff,
                "fd2": l_ref["fd2"], "fd19": l_ref["fd19"],
            })

        v_cands_hint = v_by_key.get((target_op, target_cs, target_orig, target_dest)) or v_by_key.get((op, cs, target_orig, target_dest))
        rec_fids, fleet_tier, grade_label, grade_note = compute_recommended_fleets(
            target_op, target_orig, target_dest, lrows, fr_fids, lrows[0]["route_type"], v_cands_hint
        )

        gkey = (target_op, target_cs, target_orig, target_dest)
        plan_groups[gkey] = {
            "op": target_op, "cs": target_cs, "num": "CA" + target_cs[3:],
            "raw_op": op, "raw_cs": cs, "raw_orig": orig, "raw_dest": dest,
            "orig": target_orig, "dest": target_dest,
            "route_type": lrows[0]["route_type"], "grp": lrows[0]["grp"],
            "promoted_expired": any(l.get("_promoted_from_expired") for l in lrows),
            "raw_slots": raw_slots, "raw_legs": lrows,
            "fr_kind": kind, "fr_info": info, "fr_acts": sum(1 for o in cands if o["is_actual"]),
            "fr_obs": len(cands), "fr_last_act": last_act, "fr_pairs": fr_pairs, "fr_cl": fr_cl,
            "fr_ac_cnt": ac_cnt, "fr_fids": fr_fids, "rec_fids": rec_fids,
            "fleet_tier": fleet_tier, "grade_label": grade_label, "grade_note": grade_note,
            "carrier_note": carrier_note, "ap_note": ap_note, "tz_issues": tz_issues,
        }

    # Match Plan Groups against VAMSYS Existing Routes (authoritative VAMSYS airport codes)
    matched_v_ids = set()
    add_plan_rows = []
    adj_rows = []
    keep_clean_rows = []
    redundant_slot_deactivates = []
    extra_slot_adds = []
    stop_rows = []

    for gkey, pg in sorted(plan_groups.items()):
        op, cs, orig, dest = gkey
        v_cands = list(v_by_key.get((op, cs, orig, dest), []))
        matched_via_old_op = False
        if not v_cands and (pg["raw_op"], pg["raw_cs"]) != (op, cs):
            v_cands = list(v_by_key.get((pg["raw_op"], pg["raw_cs"], orig, dest), []))
            if v_cands:
                matched_via_old_op = True

        k_fr = pg["fr_kind"]
        if k_fr in ("direct_actual", "direct_sched"):
            fr_verdict = f"FR24换季确认({k_fr}, 实飞={pg['fr_acts']})"
            is_fr_confirmed = True
        elif k_fr in ("pre_winter_stopped", "pre_winter_op_mismatch", "pre_winter_other_route"):
            fr_verdict = f"FR24换季后停飞({k_fr}, 末次={pg['fr_info'].get('last_date','')})"
            is_fr_confirmed = False
        elif k_fr == "zero_in_365d":
            fr_verdict = "FR24近365天0执飞"
            is_fr_confirmed = False
        elif k_fr == "route_mismatch":
            fr_verdict = f"FR24飞他段({pg['fr_info'].get('other_ods', [])[:2]})"
            is_fr_confirmed = False
        else:
            fr_verdict = f"FR24({k_fr})"
            is_fr_confirmed = False

        # Calibrate slots against FR24 clusters (eliminating fake Pre-Plan weekday micro-splits & stale times/days!)
        cal_slots = calibrate_slots_with_fr24(pg["raw_slots"], pg["fr_cl"], is_fr_confirmed, pg["promoted_expired"], v_cands)
        pg["slots"] = cal_slots

        if not v_cands:
            cat_add = "A1_计划+FR24双确认_全新OD新增" if is_fr_confirmed else "B_计划有但FR24未飞/停飞(建议暂缓核批复)"
            for idx, sl in enumerate(cal_slots, 1):
                add_plan_rows.append({
                    "cat": cat_add, "slot_idx": f"{idx}/{len(cal_slots)}",
                    "op": op, "cs": cs, "fn": pg["num"], "type": pg["route_type"],
                    "orig": orig, "dest": dest, "grp": pg["grp"],
                    "rec_dep": sl["dep"], "rec_arr": sl["arr"], "rec_vdays": sl["vdays"], "rec_fd3_utc": sl["fd3_utc"],
                    "plan_utc": f"{sl['plan_fd3']} {sl['plan_dep']}-{sl['plan_arr']}" if sl["plan_dep"] else "(FR24漏列补时段)",
                    "bjt": sl["bjt"], "fd2": sl["fd2"],
                    "rec_fids": ",".join(sorted(pg["rec_fids"])), "rec_fcodes": fleet_codes_str(pg["rec_fids"], v_fleets),
                    "fleet_tier": pg["fleet_tier"], "grade_label": pg["grade_label"],
                    "fr_verdict": fr_verdict, "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in pg["fr_pairs"][:3]),
                    "fr_acs": ", ".join(f"{k}:{v}" for k, v in pg["fr_ac_cnt"].most_common(6)),
                    "notes": " | ".join(x for x in [pg["ap_note"], pg["carrier_note"], pg["grade_note"], sl["fr_cal_note"] or sl["tz_note"]] if x),
                })
            continue

        # MATCHED IN VAMSYS: mark all v_cands as matched
        for vr in v_cands:
            matched_v_ids.add(vr["ID"])

        # If Plan + VAMSYS match, BUT FR24 proves STOPPED / 0-flight / other-route -> route to Stop Assessment instead of blindly adjusting!
        if not is_fr_confirmed:
            for vr in v_cands:
                stop_rows.append({
                    "id": vr["ID"], "op": vr["_op"], "cs": vr["_cs"], "fn": vr["_fn"], "type": vr["Type"],
                    "orig": vr["_orig"], "dest": vr["_dest"], "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"],
                    "v_fd3": day_set_to_fd3(vr["_days"]), "v_fids": ",".join(sorted(vr["_fids"])),
                    "v_fcodes": fleet_codes_str(vr["_fids"], v_fleets),
                    "action": "拟停用评估-计划仍列但FR24证实换季已停飞/0飞", "keep_flag": False, "fr_kind": k_fr,
                    "obs": pg["fr_obs"], "acts": pg["fr_acts"], "last_act": pg["fr_last_act"] or pg["fr_info"].get("last_date", pg["fr_info"].get("max_stale", "")),
                    "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in pg["fr_pairs"][:2]),
                    "fr_acs": ", ".join(f"{k}:{v}" for k, v in pg["fr_ac_cnt"].most_common(5)),
                    "detail": f"原计划{cal_slots[0]['plan_fd3']} {cal_slots[0]['plan_dep']}-{cal_slots[0]['plan_arr']} | {pg['fr_info']}",
                })
            continue

        # Greedy best pairing between VAMSYS IDs and FR24-Calibrated Target Slots
        used_slots = set()
        used_v = set()
        scored = []
        for vi, vr in enumerate(v_cands):
            for si_idx, sl in enumerate(cal_slots):
                sc = 0
                if vr["_dep_min"] is not None:
                    sc += abs(circ_diff_min(vr["_dep_min"], parse_hhmm(sl["dep"])))
                else:
                    sc += 300
                if vr["_days"] != sl["days"]:
                    sc += 200 * len(vr["_days"] ^ sl["days"])
                scored.append((sc, vi, si_idx))
        scored.sort()
        slot_by_v = {}
        for _, vi, si_idx in scored:
            if vi not in used_v and si_idx not in used_slots:
                used_v.add(vi)
                used_slots.add(si_idx)
                slot_by_v[vi] = cal_slots[si_idx]

        for vi, vr in enumerate(v_cands):
            sl = slot_by_v.get(vi)
            if sl is None:
                redundant_slot_deactivates.append({
                    "id": vr["ID"], "op": vr["_op"], "cs": vr["_cs"], "orig": vr["_orig"], "dest": vr["_dest"],
                    "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"], "v_fd3": day_set_to_fd3(vr["_days"]),
                    "plan_slots_cnt": len(cal_slots), "v_slots_cnt": len(v_cands),
                    "reason": "同OD现行VAMSYS多余子时段/空壳ID（建议合并至主时段并停用多余ID）",
                })
                continue

            issues = []
            if matched_via_old_op:
                issues.append(f"承运人变更({vr['_cs']}->{cs})")
            if vr["_fn"] != pg["num"]:
                issues.append(f"班号错填({vr['_fn']}->{pg['num']})")
            if vr["Type"] != pg["route_type"]:
                issues.append(f"类型变更({vr['Type']}->{pg['route_type']})")

            tgt_dep, tgt_arr = sl["dep"], sl["arr"]
            cl_ref = sl.get("fr_cl_ref")
            if vr["_dep_min"] is None or vr["_arr_min"] is None:
                issues.append("VAMSYS起降时刻空缺")
            else:
                d_dep = abs(hm_circ_diff(vr["_dep_hm"], tgt_dep))
                d_arr = abs(hm_circ_diff(vr["_arr_hm"], tgt_arr))
                # If plan wasn't overwritten by low-act FR24 (acts==1), don't move VAMSYS if VAMSYS already matches FR24 <= 15m!
                v_matches_fr = bool(cl_ref and abs(hm_circ_diff(vr["_dep_hm"], cl_ref["mode_dep"])) <= 15 and abs(hm_circ_diff(vr["_arr_hm"], cl_ref["mode_arr"])) <= 15)
                if d_dep > 15 or d_arr > 15:
                    if v_matches_fr:
                        tgt_dep, tgt_arr = vr["_dep_hm"], vr["_arr_hm"]
                    else:
                        issues.append(f"时刻调整(dep差{d_dep}m,arr差{d_arr}m)")

            if not vr["_days"]:
                issues.append("VAMSYS班期空缺")
            elif vr["_days"] != sl["days"]:
                issues.append(f"班期调整({day_set_to_fd3(vr['_days'])}->{sl['fd3_utc']})")

            fleet_issues, legacy_note, rec_fleet_out = eval_vamsys_fleets(
                op, orig, dest, vr, v_fleets, pg["rec_fids"], pg["fleet_tier"], pg["grade_label"], pg["grade_note"]
            )
            if fleet_issues:
                issues.append("机队调整: " + "; ".join(fleet_issues))

            rec_obj = {
                "id": vr["ID"], "op": op, "cs": cs, "v_cs": vr["_cs"], "fn": pg["num"], "v_fn": vr["_fn"],
                "orig": orig, "dest": dest, "v_orig": vr["_orig"], "v_dest": vr["_dest"],
                "type": pg["route_type"], "grp": pg["grp"],
                "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"], "v_fd3": day_set_to_fd3(vr["_days"]),
                "plan_dep": sl["plan_dep"] or sl["dep"], "plan_arr": sl["plan_arr"] or sl["arr"], "plan_fd3": sl["plan_fd3"] or sl["fd3_utc"], "bjt": sl["bjt"],
                "rec_dep": tgt_dep, "rec_arr": tgt_arr, "rec_fd3": sl["fd3_utc"], "rec_vdays": sl["vdays"],
                "fd2": sl["fd2"], "fleet_tier": pg["fleet_tier"], "grade_label": pg["grade_label"],
                "v_fids": ",".join(sorted(vr["_fids"])), "v_fcodes": fleet_codes_str(vr["_fids"], v_fleets),
                "rec_fids": ",".join(sorted(rec_fleet_out)),
                "rec_fcodes": fleet_codes_str(rec_fleet_out, v_fleets),
                "issues": " | ".join(issues) if issues else "无需调整(一致)",
                "fleet_issues": "; ".join(fleet_issues),
                "legacy_note": legacy_note,
                "fr_verdict": fr_verdict,
                "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in pg["fr_pairs"][:3]),
                "fr_acs": ", ".join(f"{k}:{v}" for k, v in pg["fr_ac_cnt"].most_common(6)),
                "notes": " | ".join(x for x in [pg["carrier_note"], pg["grade_note"], sl["fr_cal_note"] or sl["tz_note"], "由10/25单日过期按FR24实飞升格保留" if pg["promoted_expired"] else ""] if x),
            }

            if issues:
                adj_rows.append(rec_obj)
            else:
                keep_clean_rows.append(rec_obj)

        for si_idx, sl in enumerate(cal_slots):
            if si_idx not in used_slots:
                extra_slot_adds.append({
                    "cat": "A2_同OD多时段补充(FR24确认独立班次)",
                    "slot_idx": f"{si_idx+1}/{len(cal_slots)}",
                    "op": op, "cs": cs, "fn": pg["num"], "type": pg["route_type"],
                    "orig": orig, "dest": dest, "grp": pg["grp"],
                    "rec_dep": sl["dep"], "rec_arr": sl["arr"], "rec_vdays": sl["vdays"], "rec_fd3_utc": sl["fd3_utc"],
                    "plan_utc": f"{sl['plan_fd3']} {sl['plan_dep']}-{sl['plan_arr']}" if sl["plan_dep"] else "(FR24漏列补时段)",
                    "bjt": sl["bjt"], "fd2": sl["fd2"],
                    "rec_fids": ",".join(sorted(pg["rec_fids"])), "rec_fcodes": fleet_codes_str(pg["rec_fids"], v_fleets),
                    "fleet_tier": pg["fleet_tier"], "grade_label": pg["grade_label"], "fr_verdict": fr_verdict,
                    "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in pg["fr_pairs"][:3]),
                    "fr_acs": ", ".join(f"{k}:{v}" for k, v in pg["fr_ac_cnt"].most_common(6)),
                    "notes": " | ".join(x for x in [f"VAMSYS已有{len(v_cands)}行，本时段为FR24确认独立班次", pg["grade_note"], sl["fr_cal_note"]] if x),
                })

    # Partition VAMSYS commercial routes NOT matched by Plan into:
    #   (A) FR24-Active Plan-Omitted Routes -> keep & calibrate against FR24 Reality (adj_rows / keep_clean_rows)
    #   (B) Confirmed Stop / Cleanup Routes -> stop_rows
    fr_active_unplanned_v = defaultdict(list)
    gap_hold_rows = []
    for vr in comm_v_routes:
        if vr["ID"] in matched_v_ids:
            continue
        op, cs, orig, dest = vr["_op"], vr["_cs"], vr["_orig"], vr["_dest"]
        kind, cands, info = probe(op, cs, orig, dest)
        fr_pairs, fr_fids, ac_cnt, has_nov, last_act, _ = extract_fr24_slots_and_fleets(op, cands)
        acts_n = sum(1 for o in cands if o["is_actual"])

        num = "CA" + cs[3:] if cs.startswith(TARGET_OPS) else cs
        alt_in_plan = [g for gk, g in plan_groups.items() if g["num"] == num and gk[2] == orig and gk[3] == dest and gk[0] != op]
        alt_fr_acts = sum(1 for o in fr["by_num_od"].get((num, orig, dest), []) if o["op"] != op and o["is_actual"])

        if kind in ("direct_actual", "direct_sched") and not (alt_in_plan and acts_n <= 2 and alt_fr_acts >= 5):
            fr_active_unplanned_v[(op, cs, orig, dest)].append((vr, kind, cands, info))
            gk = (op, cs, orig, dest)
            if gk in rotation_gaps:
                rotation_gaps[gk].update(status="VAMSYS已有·FR24换季实飞确认→按实飞保留/校准", acts=acts_n, obs=len(cands),
                                         last_act=last_act, v_ids=",".join(filter(None, [rotation_gaps[gk]["v_ids"], vr["ID"]])))
            continue

        gk = (op, cs, orig, dest)
        if gk in rotation_gaps:
            # FR24 publishes these continuation legs mostly WITHOUT a flight number, so "no winter observation" is a
            # data blind spot, not proof of suspension: hold for review instead of retiring.
            g = rotation_gaps[gk]
            g["status"] = "VAMSYS已有·FR24冬季无观测→暂缓停用待核(不改动)"
            g["v_ids"] = ",".join(filter(None, [g["v_ids"], vr["ID"]]))
            g["detail"] = f"现行{vr['_dep_hm']}-{vr['_arr_hm']} {day_set_to_fd3(vr['_days'])} | FR24={kind} {info}"
            gap_hold_rows.append(vr)
            continue

        if kind in ("direct_actual", "direct_sched"):
            action = "可停用-旧承运偶发代飞交接(主承运已入冬春)"
        elif kind == "op_mismatch":
            action = "确认可停用-FR24确认换承运人"
        elif kind == "route_mismatch":
            action = "确认可停用-同呼号已换航段"
        elif kind == "stale_sched_only":
            action = "确认可停用-换季初占位排班11/04前已消失"
        elif kind in ("pre_winter_stopped", "pre_winter_op_mismatch", "pre_winter_other_route"):
            action = "确认可停用-夏秋有飞10/25后已停飞"
        else:
            action = f"确认可停用-近365天0执飞({kind})"

        stop_rows.append({
            "id": vr["ID"], "op": op, "cs": cs, "fn": vr["_fn"], "type": vr["Type"],
            "orig": orig, "dest": dest, "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"],
            "v_fd3": day_set_to_fd3(vr["_days"]), "v_fids": ",".join(sorted(vr["_fids"])),
            "v_fcodes": fleet_codes_str(vr["_fids"], v_fleets),
            "action": action, "keep_flag": False, "fr_kind": kind,
            "obs": len(cands), "acts": acts_n, "last_act": last_act or info.get("last_date", info.get("max_stale", "")),
            "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in fr_pairs[:2]),
            "fr_acs": ", ".join(f"{k}:{v}" for k, v in ac_cnt.most_common(5)),
            "detail": str(info),
        })

    # Reconcile FR24-Active Plan-Omitted VAMSYS Routes directly against FR24 Reality!
    fr_protected_adj_cnt = 0
    fr_protected_keep_cnt = 0
    for (op, cs, orig, dest), items in sorted(fr_active_unplanned_v.items()):
        v_cands = [x[0] for x in items]
        _, kind, cands, _ = items[0]
        fr_pairs, fr_fids, ac_cnt, _, _, _ = extract_fr24_slots_and_fleets(op, cands)
        fr_cl = cluster_fr24_slots(cands)
        acts_n = sum(1 for o in cands if o["is_actual"])
        rt_type = "cargo" if (op == "CAO" or any(vr["Type"] == "cargo" for vr in v_cands)) else "scheduled"
        rec_fids, fleet_tier, grade_label, grade_note = compute_recommended_fleets(op, orig, dest, [], fr_fids, rt_type, v_cands)
        fr_verdict = f"FR24换季确认({kind}, 实飞={acts_n}) [计划漏列·FR24保飞]"

        # Keep valid active clusters (acts>=2 or Nov obs>=2, or fallback primary cluster if none)
        active_banks = [c for c in fr_cl if c["acts"] >= 2 or c["nov_obs"] >= 2] or fr_cl[:1]
        if not active_banks:
            # Synthesize from VAMSYS primary if no sched pairs exist on airport-only board rows
            for vr in v_cands:
                fleet_issues, legacy_note, rec_fleet_out = eval_vamsys_fleets(op, orig, dest, vr, v_fleets, rec_fids, fleet_tier, grade_label, grade_note)
                issues = []
                if vr["_dep_min"] is None or vr["_arr_min"] is None:
                    issues.append("VAMSYS起降时刻空缺")
                if not vr["_days"]:
                    issues.append("VAMSYS班期空缺")
                if fleet_issues:
                    issues.append("机队调整: " + "; ".join(fleet_issues))
                rec_obj = {
                    "id": vr["ID"], "op": op, "cs": cs, "v_cs": cs, "fn": "CA" + cs[3:] if cs.startswith(TARGET_OPS) else cs, "v_fn": vr["_fn"],
                    "orig": orig, "dest": dest, "v_orig": orig, "v_dest": dest, "type": rt_type, "grp": "计划漏列",
                    "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"], "v_fd3": day_set_to_fd3(vr["_days"]),
                    "plan_dep": "", "plan_arr": "", "plan_fd3": "", "bjt": "(计划表漏列)",
                    "rec_dep": vr["_dep_hm"], "rec_arr": vr["_arr_hm"], "rec_fd3": day_set_to_fd3(vr["_days"]), "rec_vdays": day_set_to_vdays(vr["_days"]),
                    "fd2": "", "fleet_tier": fleet_tier, "grade_label": grade_label,
                    "v_fids": ",".join(sorted(vr["_fids"])), "v_fcodes": fleet_codes_str(vr["_fids"], v_fleets),
                    "rec_fids": ",".join(sorted(rec_fleet_out)), "rec_fcodes": fleet_codes_str(rec_fleet_out, v_fleets),
                    "issues": " | ".join(issues) if issues else "计划漏列·FR24保飞且与现行一致",
                    "fleet_issues": "; ".join(fleet_issues), "legacy_note": legacy_note, "fr_verdict": fr_verdict,
                    "fr_pairs": "", "fr_acs": ", ".join(f"{k}:{v}" for k, v in ac_cnt.most_common(6)),
                    "notes": " | ".join(x for x in ["计划表未收录，按FR24实飞保留；缺FR24具体时刻仅核校机队/空值", grade_note] if x),
                }
                if issues:
                    adj_rows.append(rec_obj)
                    fr_protected_adj_cnt += 1
                else:
                    keep_clean_rows.append(rec_obj)
                    fr_protected_keep_cnt += 1
            continue

        # Pair v_cands against FR24 active banks
        used_v = set()
        used_b = set()
        scored = []
        for vi, vr in enumerate(v_cands):
            for bi, cl in enumerate(active_banks):
                sc = 0
                bank_days = cl["act_days"] or cl["days"]
                if vr["_dep_min"] is not None:
                    sc += abs(circ_diff_min(vr["_dep_min"], parse_hhmm(cl["mode_dep"])))
                else:
                    sc += 300
                if vr["_days"] != bank_days:
                    sc += 200 * len(vr["_days"] ^ bank_days)
                scored.append((sc, vi, bi))
        scored.sort()
        bank_by_v = {}
        for _, vi, bi in scored:
            if vi not in used_v and bi not in used_b:
                used_v.add(vi)
                used_b.add(bi)
                bank_by_v[vi] = active_banks[bi]

        for vi, vr in enumerate(v_cands):
            cl = bank_by_v.get(vi)
            if cl is None:
                redundant_slot_deactivates.append({
                    "id": vr["ID"], "op": op, "cs": cs, "orig": orig, "dest": dest,
                    "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"], "v_fd3": day_set_to_fd3(vr["_days"]),
                    "plan_slots_cnt": len(active_banks), "v_slots_cnt": len(v_cands),
                    "reason": "计划漏列但FR24保飞OD内多余拆班/空壳ID（建议合并至主班次并清理多余ID）",
                })
                continue

            bank_days = cl["act_days"] or cl["days"]
            tgt_dep, tgt_arr = cl["mode_dep"], cl["mode_arr"]
            tgt_days = set(vr["_days"]) if (cl["acts"] < 10 and vr["_days"] and bank_days.issubset(vr["_days"])) else bank_days
            tgt_fd3 = day_set_to_fd3(tgt_days)

            issues = []
            tgt_fn = "CA" + cs[3:] if cs.startswith(TARGET_OPS) else cs
            if vr["_fn"] != tgt_fn:
                issues.append(f"班号错填({vr['_fn']}->{tgt_fn})")
            if vr["_dep_min"] is None or vr["_arr_min"] is None:
                issues.append("VAMSYS起降时刻空缺")
            else:
                dd = abs(hm_circ_diff(vr["_dep_hm"], tgt_dep))
                da = abs(hm_circ_diff(vr["_arr_hm"], tgt_arr))
                if dd > 15 or da > 15:
                    issues.append(f"时刻调整(按FR24校准, dep差{dd}m,arr差{da}m)")
            if not vr["_days"]:
                issues.append("VAMSYS班期空缺")
            elif vr["_days"] != tgt_days:
                issues.append(f"班期调整(按FR24校准 {day_set_to_fd3(vr['_days'])}->{tgt_fd3})")

            fleet_issues, legacy_note, rec_fleet_out = eval_vamsys_fleets(op, orig, dest, vr, v_fleets, rec_fids, fleet_tier, grade_label, grade_note)
            if fleet_issues:
                issues.append("机队调整: " + "; ".join(fleet_issues))

            rec_obj = {
                "id": vr["ID"], "op": op, "cs": cs, "v_cs": cs, "fn": tgt_fn, "v_fn": vr["_fn"],
                "orig": orig, "dest": dest, "v_orig": orig, "v_dest": dest, "type": rt_type, "grp": "计划漏列",
                "v_dep": vr["_dep_hm"], "v_arr": vr["_arr_hm"], "v_fd3": day_set_to_fd3(vr["_days"]),
                "plan_dep": "(计划漏列)", "plan_arr": "", "plan_fd3": "", "bjt": "(原计划未收录，按FR24实飞维护)",
                "rec_dep": tgt_dep, "rec_arr": tgt_arr, "rec_fd3": tgt_fd3, "rec_vdays": day_set_to_vdays(tgt_days),
                "fd2": "", "fleet_tier": fleet_tier, "grade_label": grade_label,
                "v_fids": ",".join(sorted(vr["_fids"])), "v_fcodes": fleet_codes_str(vr["_fids"], v_fleets),
                "rec_fids": ",".join(sorted(rec_fleet_out)), "rec_fcodes": fleet_codes_str(rec_fleet_out, v_fleets),
                "issues": " | ".join(["计划漏列·FR24保飞校准"] + issues) if issues else "计划漏列·FR24保飞且与现行一致",
                "fleet_issues": "; ".join(fleet_issues), "legacy_note": legacy_note, "fr_verdict": fr_verdict,
                "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in fr_pairs[:3]),
                "fr_acs": ", ".join(f"{k}:{v}" for k, v in ac_cnt.most_common(6)),
                "notes": " | ".join(x for x in [f"原预计划表遗漏该在飞航线，已按FR24({acts_n}实飞)直接核校维护", grade_note] if x),
            }
            if issues:
                adj_rows.append(rec_obj)
                fr_protected_adj_cnt += 1
            else:
                keep_clean_rows.append(rec_obj)
                fr_protected_keep_cnt += 1

    # Find FR24 active unplanned additions (active in FR24 >=10/25, NOT in plan_groups and NOT in v_routes)
    plan_keys_all = set(plan_groups.keys()) | {(g["raw_op"], g["raw_cs"], g["raw_orig"], g["raw_dest"]) for g in plan_groups.values()}
    v_keys_all = {(vr["_op"], vr["_cs"], vr["_orig"], vr["_dest"]) for vr in v_routes}
    unplanned_grouped = defaultdict(list)
    for o in fr["obs"]:
        if o["op_source"] == "airport_iata_fallback":
            continue
        k = (o["op"], o["callsign"], o["orig"], o["dest"])
        if k not in plan_keys_all and k not in v_keys_all:
            unplanned_grouped[k].append(o)

    unplanned_add_recs = []
    add_fr24_promoted_rows = []
    for k, obs_k in sorted(unplanned_grouped.items()):
        op, cs, orig, dest = k
        acts = [o for o in obs_k if o["is_actual"]]
        if not acts:
            continue
        digits = re.sub(r"^[A-Z]+", "", cs)
        if orig == dest or not digits or digits.startswith("0") or len(digits) <= 2:
            continue
        fr_pairs, fr_fids, ac_cnt, _, last_act, fr_days = extract_fr24_slots_and_fleets(op, obs_k)
        fr_cl = cluster_fr24_slots(obs_k)
        rt_type = "cargo" if op == "CAO" else "scheduled"
        rec_fids, tier, grade_label, grade_note = compute_recommended_fleets(
            op, orig, dest, [{"fd2": ac, "fd19": ""} for ac in ac_cnt], fr_fids, rt_type
        )
        ap_missing = ",".join(a for a in (orig, dest) if a not in v_airports)
        is_gap = k in rotation_gaps
        if is_gap:
            rotation_gaps[k].update(status=f"FR24换季实飞{len(acts)}班→按实飞时刻/星期新增", acts=len(acts), obs=len(obs_k), last_act=last_act)
        tier_cat = "C1_FR24高频/多次补漏新增(acts>=2)" if len(acts) >= 2 else "C2_FR24单次实飞/包机待观察(acts=1)"
        unplanned_add_recs.append({
            "cat": tier_cat, "op": op, "cs": cs, "fn": "CA" + cs[3:] if cs.startswith(TARGET_OPS) else cs,
            "type": rt_type, "orig": orig, "dest": dest, "acts": len(acts), "obs": len(obs_k),
            "last_act": last_act, "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in fr_pairs[:3]),
            "fr_acs": ", ".join(f"{k}:{v}" for k, v in ac_cnt.most_common(5)),
            "rec_fids": ",".join(sorted(rec_fids)), "rec_fcodes": fleet_codes_str(rec_fids, v_fleets),
            "tier": tier, "grade_label": grade_label, "ap_missing": ap_missing,
        })
        # A plan rotation gap leg needs only 1 real flight: the plan already proves the aircraft must fly X->Y
        if len(acts) >= 2 or is_gap:
            # Promote to primary Sheet 1 新增清单 per active FR24 bank!
            banks = [c for c in fr_cl if c["acts"] >= 2] or [c for c in fr_cl if c["acts"] >= 1][:1] or fr_cl[:1]
            if not banks:
                banks = [{"mode_dep": "", "mode_arr": "", "act_days": fr_days, "days": fr_days}]
            else:
                # One weekday -> one bank: give each flown weekday to the bank with most actuals on that weekday, so
                # the same flight never appears twice on one day (e.g. CAO3155 ESSA->EBLG drifts 07:25/13:05/15:10
                # with the inbound delay: Wed is mostly 07:25, Fri/Sun mostly ~13:05).
                all_act_days = set().union(*(c["act_days"] for c in fr_cl))
                won = defaultdict(set)
                for wd in all_act_days:
                    best = max(range(len(banks)), key=lambda i: (banks[i]["day_acts"][wd], banks[i]["acts"]))
                    won[best].add(wd)
                banks = [dict(bk, act_days=won[i]) for i, bk in enumerate(banks) if won[i]]
            num = "CA" + cs[3:] if cs.startswith(TARGET_OPS) else cs
            same_num_other = Counter(o["op"] for o in fr["by_num_od"].get((num, orig, dest), []) if o["is_actual"] and o["op"] != op and o["op_source"] != "airport_iata_fallback")
            wet_note = f"多承运湿租共存(他司{dict(same_num_other)})" if same_num_other else ""
            for b_i, bk in enumerate(banks, 1):
                udays = bk.get("act_days") or bk["days"] or fr_days
                add_fr24_promoted_rows.append({
                    "cat": "C_FR24实飞补漏新增(计划去回程缺境外段)" if is_gap else "C_FR24实飞补漏新增(计划漏列·acts>=2)",
                    "slot_idx": f"{b_i}/{len(banks)}",
                    "op": op, "cs": cs, "fn": num, "type": rt_type, "orig": orig, "dest": dest, "grp": "计划漏列(FR24实飞)",
                    "rec_dep": bk["mode_dep"], "rec_arr": bk["mode_arr"], "rec_vdays": day_set_to_vdays(udays), "rec_fd3_utc": day_set_to_fd3(udays),
                    "plan_utc": "(计划表漏列)", "bjt": "(按FR24实飞补入)", "fd2": "",
                    "rec_fids": ",".join(sorted(rec_fids)), "rec_fcodes": fleet_codes_str(rec_fids, v_fleets),
                    "fleet_tier": tier, "grade_label": grade_label,
                    "fr_verdict": f"FR24换季实飞({len(acts)}班/{len(obs_k)}观测, 末次={last_act})",
                    "fr_pairs": ", ".join(f"{a}-{b}[{d}]({c})" for a, b, c, d in fr_pairs[:3]),
                    "fr_acs": ", ".join(f"{k}:{v}" for k, v in ac_cnt.most_common(6)),
                    "notes": " | ".join(x for x in [
                        f"VAMSYS缺场 {ap_missing}" if ap_missing else "", wet_note, grade_note,
                        (f"计划去程{cs}止于{orig}、回程{rotation_gaps[k]['ret_cs']}始于{dest}，表内缺此境外段；时刻星期全按FR24实飞"
                         + ("（仅1班样本，星期可能不全）" if len(acts) == 1 else "")) if is_gap else "预计划表漏列但FR24高频实飞",
                    ] if x),
                })

    # Rotation gaps with neither a VAMSYS route nor a winter FR24 actual: report only, never estimate
    for (op, cs, x, y), g in rotation_gaps.items():
        if g["status"]:
            continue
        nums = {"CA" + g["out_cs"][3:], "CA" + g["ret_cs"][3:]}
        hist_rows = [r for n in nums for r in fr["fh"].get(n, {}).get("rows", [])]
        hist_rows += [r for p in fr["reg_hist"].values() for r in p.get("rows", []) if (r.get("callsign") or "").upper() == cs]
        hist = list({r.get("id") or id(r): r for r in hist_rows
                     if r.get("origin_icao") == x and r.get("dest_icao") == y and row_is_actual(r)}.values())
        last = max((int(r.get("real_dep") or r.get("sched_dep") or 0) for r in hist), default=0)
        sched_only = len(fr["by_op_cs_od"].get((op, cs, x, y), []))
        g["status"] = ("FR24冬季无实飞→未补(已核注册号历史)" if fr["reg_hist"]
                       else "FR24冬季无实飞→未补(需按注册号补抓)")
        g["detail"] = f"365天该段实飞{len(hist)}班" + (f"，末次{fmt_date_ts(last)}" if last else "") + (f"；冬季仅排班{sched_only}条无实飞" if sched_only else "")

    # Fleet & Aircraft Usage Summary across 37 VAMSYS Fleets
    v_ac_cnt = Counter(a["Fleet ID"] for a in v_aircraft)
    v_rt_cnt = Counter()
    for vr in v_routes:
        for fid in vr["_fids"]:
            v_rt_cnt[fid] += 1
    fr_win_ac_cnt = Counter(o["ac"] for o in fr["obs"] if o["is_actual"] and o.get("ac"))
    fleet_audit_rows = []
    for fid, f in sorted(v_fleets.items(), key=lambda kv: (kv[1].get("Parameter IDs", ""), kv[1]["Type Code"], kv[0])):
        tc = f["Type Code"]
        fr_hits = sum(cnt for ac_c, cnt in fr_win_ac_cnt.items() if (
            map_ac_to_fleet("CAO" if f["Type (pax/cargo/...)"] == "cargo" else "CCA", ac_c) == fid
        ))
        status = "主力在飞(FR24有实飞)" if fr_hits > 0 or fid in ("26718", "26719") else ("空军特勤(不入商业航线)" if "10294" in f.get("Parameter IDs", "") else "纳管历史/虚拟机型(按规则不区分是否现役，按飞行区等级纳入对应池)")
        if fid == "26728":
            status = "异常跨权脏机队(Dash8，按规则继续剔除)"
        fleet_audit_rows.append([
            fid, tc, f["Type (pax/cargo/...)"], f.get("Parameter IDs", ""), f["Name"],
            v_ac_cnt.get(fid, 0), v_rt_cnt.get(fid, 0), fr_hits, status
        ])

    # Write Multi-Sheet Excel Deliverable
    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "总览与口径说明"
    ws0.append(["核查维度", "指标项", "数值", "说明"])
    ws0.append(["原始预先计划(.xls/.DB)", "原始CA*/四司总行数", len(raw_rows), "国内2824 + 国际587 + 港澳台64（1:1一致于.DB）"])
    n_exp = sum(1 for l in legs if l["expired"])
    ws0.append(["原始预先计划(.xls/.DB)", "展开物理航段行", n_raw_legs, f"其中{n_exp}条为10/25单日行，其余{n_raw_legs - n_exp}条为全季行"])
    ws0.append(["原始预先计划(.xls/.DB)", "去回程不连续(计划表缺境外段)", f"{len(rotation_gaps)}组",
                f"{dict(Counter(g['status'].split('→')[0] for g in rotation_gaps.values()))}；只按FR24实飞补入，不推算；VAMSYS已有但FR24无观测的{len(gap_hold_rows)}条暂缓停用(见Sheet7)"])
    ws0.append(["FR24-First校准口", "归一化计划航段组(去重OD)", len(plan_groups), "已包含1025单日FR24高飞升格(CCA754/796/890/974) + FR24消歧合并微拆班与错位时刻/星期校准"])
    ws0.append(["VAMSYS当前库(rev 484)", "机场 / 机队 / 飞机 / 航路", f"{len(v_airports)} / {len(v_fleets)} / {len(v_aircraft)} / 15", f"当前航线总计 {len(v_routes)} 条 (商业比较 {len(comm_v_routes)} 条 + 特殊保留 {len(special_v_routes)} 条)"])
    ws0.append(["FR24 Gold 全量数据", "Gold 365天单班缓存 / 机场看板 / 搜索", f"{fr['fh_count']} / {fr['ap_count']} / {fr['search_count']}", f"换季统一观测 {len(fr['obs'])} 条 (实际执飞 {sum(1 for o in fr['obs'] if o['is_actual'])} 条)"])
    ws0.append(["重算结果-新增", "A1 计划+FR24双确认全新OD / B 计划停飞暂缓", len(add_plan_rows), str(Counter(x['cat'] for x in add_plan_rows))])
    ws0.append(["重算结果-新增", "A2 同OD真实多时段补充(剔除计划微拆班)", len(extra_slot_adds), str(Counter(x['cat'] for x in extra_slot_adds))])
    ws0.append(["重算结果-新增", "C FR24实飞补漏新增(计划漏列·acts>=2并入Sheet1)", len(add_fr24_promoted_rows), f"Sheet4计划外汇总 {len(unplanned_add_recs)} 条 (含 {sum(1 for x in unplanned_add_recs if x['acts']==1)} 条单次包机待观察)"])
    ws0.append(["重算结果-调整与保持", "VAMSYS现行ID完全一致无需调整(Sheet6)", len(keep_clean_rows), f"含计划有+FR24确认与VAMSYS一致，以及计划漏列但FR24证实与VAMSYS一致 {fr_protected_keep_cnt} 条"])
    ws0.append(["重算结果-调整与保持", "VAMSYS现行ID需按FR24/计划校准调整(Sheet2)", len(adj_rows), f"全为FR24实证在飞航线：含机队调整 {sum(1 for x in adj_rows if x['fleet_issues'])} 条；含计划漏列·FR24保飞校准 {fr_protected_adj_cnt} 条"])
    ws0.append(["重算结果-调整与保持", "同OD多余拆班/空壳ID待清理(Sheet3)", len(redundant_slot_deactivates), "剔除了FR24证实为独立多班次的现行ID，仅余真正重复拆班/空壳"])
    ws0.append(["重算结果-拟停用", "拟停用候选总条数(Sheet3)", len(stop_rows), str(Counter(x['action'] for x in stop_rows))])
    ws0.append(["重算结果-机场与机型口径", "VAMSYS机场缺场与机场/机型选择逻辑", "仅缺 ZBAL (2400x30m 3C)", "机场代码按VAMSYS口径(ZSGS/ZPMS/ZMCK/UZTT保留现行，FR24的ZSJA/ZPLX已映射回ZSGS/ZPMS)；机型按[计划FD2+FR24定宽窄体 -> 全量Param9671/9677/9678/9679机队(不区分现役，含B752/B762/B763/B742/A339/A343/A346/B772，剔除DH8D)按3C/4C/4D/4E/4F匹配]"])

    def write_sheet(title, headers, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for row in rows:
            ws.append(row)

    all_sheet1_adds = (
        sorted([x for x in add_plan_rows if x["cat"].startswith("A1")], key=lambda r: (r["op"], r["cs"], r["orig"]))
        + sorted(add_fr24_promoted_rows, key=lambda r: (r["op"], r["cs"], r["orig"]))
        + sorted(extra_slot_adds, key=lambda r: (r["op"], r["cs"], r["orig"]))
        + sorted([x for x in add_plan_rows if x["cat"].startswith("B_")], key=lambda r: (r["op"], r["cs"], r["orig"]))
    )
    write_sheet(
        "1_新增清单_FR24校准",
        ["导入建议分级", "航司", "呼号", "班号", "类型", "始发", "到达", "类别", "时段序号",
         "推荐UTC起飞", "推荐UTC到达", "推荐UTC星期(VAMSYS)", "推荐UTC星期(Bitmask)",
         "计划原始BJT/说明", "计划主机型FD2", "宽窄体判定", "起降飞行区等级", "推荐Fleet IDs", "推荐机型代码",
         "FR24 Gold结论", "FR24实飞UTC样本", "FR24实飞机型分布", "前置阻塞/校准说明"],
        [[x["cat"], x["op"], x["cs"], x["fn"], x["type"], x["orig"], x["dest"], x["grp"], x["slot_idx"],
          x["rec_dep"], x["rec_arr"], x["rec_vdays"], x["rec_fd3_utc"], x["bjt"], x["fd2"], x["fleet_tier"], x["grade_label"],
          x["rec_fids"], x["rec_fcodes"], x["fr_verdict"], x["fr_pairs"], x["fr_acs"], x["notes"]]
         for x in all_sheet1_adds],
    )

    write_sheet(
        "2_调整清单_FR24校准与机队",
        ["VAMSYS_ID", "航司", "目标呼号", "目标班号", "始发", "到达", "类型", "来源/类别",
         "调整判定明细", "机队问题明细",
         "现行UTC起飞", "现行UTC到达", "现行UTC星期",
         "原计划推算UTC起飞", "原计划推算UTC到达", "原计划推算UTC星期", "原计划BJT",
         "推荐更新UTC起飞(FR24校准)", "推荐更新UTC到达(FR24校准)", "推荐更新UTC星期",
         "宽窄体判定", "起降飞行区等级", "现行Fleet IDs", "现行机型代码", "推荐Fleet IDs", "推荐机型代码",
         "FR24 Gold结论", "FR24实飞UTC样本", "FR24实飞机型分布", "备注"],
        [[x["id"], x["op"], x["cs"], x["fn"], x["orig"], x["dest"], x["type"], x["grp"],
          x["issues"], x["fleet_issues"],
          x["v_dep"], x["v_arr"], x["v_fd3"],
          x["plan_dep"], x["plan_arr"], x["plan_fd3"], x["bjt"],
          x["rec_dep"], x["rec_arr"], x["rec_fd3"],
          x["fleet_tier"], x["grade_label"], x["v_fids"], x["v_fcodes"], x["rec_fids"], x["rec_fcodes"],
          x["fr_verdict"], x["fr_pairs"], x["fr_acs"], x["notes"]]
         for x in adj_rows],
    )

    write_sheet(
        "3_拟停用清单_FR24守门",
        ["VAMSYS_ID", "航司", "呼号", "班号", "类型", "始发", "到达",
         "现行UTC起飞", "现行UTC到达", "现行UTC星期", "现行Fleet IDs", "现行机型代码",
         "FR24重算处置建议", "是否在飞保飞", "FR24状态分类", "换季观测数", "换季实飞数", "末次实飞/排班日期",
         "FR24实飞UTC样本", "FR24实飞机型", "补充详情"],
        [[x["id"], x["op"], x["cs"], x["fn"], x["type"], x["orig"], x["dest"],
          x["v_dep"], x["v_arr"], x["v_fd3"], x["v_fids"], x["v_fcodes"],
          x["action"], "否(可停用/待核)", x["fr_kind"],
          x["obs"], x["acts"], x["last_act"], x["fr_pairs"], x["fr_acs"], x["detail"]]
         for x in sorted(stop_rows, key=lambda r: (r["action"], r["op"], r["cs"], r["orig"]))]
        + [[x["id"], x["op"], x["cs"], "CA" + x["cs"][3:], "scheduled/cargo", x["orig"], x["dest"],
            x["v_dep"], x["v_arr"], x["v_fd3"], "", "", x["reason"], "否(同OD多余ID清理)", "redundant_slot",
            0, 0, "", "", "", f"target_slots={x['plan_slots_cnt']} vamsys={x['v_slots_cnt']}"]
           for x in redundant_slot_deactivates],
    )

    write_sheet(
        "4_FR24实飞计划外新增明细",
        ["分类", "航司", "呼号", "班号", "类型", "始发", "到达", "换季实飞数", "换季总观测", "末次实飞日期",
         "FR24实飞UTC样本", "FR24实飞机型", "宽窄体判定", "起降飞行区等级", "推荐Fleet IDs", "推荐机型代码", "VAMSYS缺场"],
        [[x["cat"], x["op"], x["cs"], x["fn"], x["type"], x["orig"], x["dest"],
          x["acts"], x["obs"], x["last_act"], x["fr_pairs"], x["fr_acs"],
          x["tier"], x["grade_label"], x["rec_fids"], x["rec_fcodes"], x["ap_missing"]]
         for x in sorted(unplanned_add_recs, key=lambda r: (-r["acts"], r["op"], r["cs"]))],
    )

    write_sheet(
        "5_VAMSYS机队与特殊航线审计",
        ["表块", "ID/呼号", "Type Code/航司", "类型", "Parameter IDs / 始发->到达", "名称 / 说明", "机尾数量/时间", "使用航线数/标签", "FR24实飞数/结论"],
        [["VAMSYS机队", *row] for row in fleet_audit_rows]
        + [["特殊保留航线(不入停用)", vr["ID"], vr["_op"], vr["Type"], f"{vr['_orig']}->{vr['_dest']}", f"{vr['_cs']} ({vr['_fn']})", f"{vr['_dep_hm']}-{vr['_arr_hm']}", vr.get("Tags", ""), vr.get("Remarks", "")] for vr in special_v_routes]
        + [["1025单日行处置", x["cs"], x["cs"][:3], "expired", f"{x['orig']}->{x['dest']}", f"同OD另有常驻={x['has_reg']}", f"acts={x['acts']} last={x['last_act']}", x["kind"], x["decision"]] for x in expired_audit],
    )

    write_sheet(
        "6_完全一致无需调整(参考)",
        ["VAMSYS_ID", "航司", "呼号", "始发", "到达", "现行UTC起飞", "现行UTC到达", "现行UTC星期", "宽窄体判定", "起降飞行区等级", "现行机型代码", "FR24状态"],
        [[x["id"], x["op"], x["cs"], x["orig"], x["dest"], x["v_dep"], x["v_arr"], x["v_fd3"], x["fleet_tier"], x["grade_label"], x["v_fcodes"], x["fr_verdict"]]
         for x in keep_clean_rows],
    )

    write_sheet(
        "7_去回程不连续核对",
        ["航司", "去程呼号", "回程呼号", "缺失境外段始发", "缺失境外段到达", "处理结论", "换季实飞数", "换季观测数", "末次实飞", "VAMSYS现行ID", "补充详情"],
        [[g["op"], g["out_cs"], g["ret_cs"], g["orig"], g["dest"], g["status"], g["acts"], g["obs"], g["last_act"], g["v_ids"], g["detail"]]
         for g in sorted(rotation_gaps.values(), key=lambda g: (g["status"], g["out_cs"]))],
    )

    wb.save(OUT_XLSX)

    summary = {
        "ws_rev": ws_rev,
        "raw_rows": len(raw_rows),
        "raw_legs": n_raw_legs,
        "rotation_gaps": [{k: v for k, v in g.items()} for g in rotation_gaps.values()],
        "gap_hold_rows": len(gap_hold_rows),
        "plan_groups": len(plan_groups),
        "vamsys_routes": len(v_routes),
        "vamsys_comm": len(comm_v_routes),
        "vamsys_special": len(special_v_routes),
        "add_plan_rows": len(add_plan_rows),
        "add_plan_by_cat": dict(Counter(x["cat"] for x in add_plan_rows)),
        "add_fr24_promoted_rows": len(add_fr24_promoted_rows),
        "extra_slot_adds": len(extra_slot_adds),
        "unplanned_add_recs": len(unplanned_add_recs),
        "unplanned_by_cat": dict(Counter(x["cat"] for x in unplanned_add_recs)),
        "keep_clean_rows": len(keep_clean_rows),
        "fr_protected_keep_cnt": fr_protected_keep_cnt,
        "adj_rows": len(adj_rows),
        "fr_protected_adj_cnt": fr_protected_adj_cnt,
        "adj_fleet_issues": sum(1 for x in adj_rows if x["fleet_issues"]),
        "adj_dh8d_issues": sum(1 for x in adj_rows if "DH8D" in x["fleet_issues"]),
        "redundant_slot_deactivates": len(redundant_slot_deactivates),
        "stop_rows": len(stop_rows),
        "stop_by_action": dict(Counter(x["action"] for x in stop_rows)),
        "v_id_accounting_total": len(keep_clean_rows) + len(adj_rows) + len(redundant_slot_deactivates) + len(stop_rows) + len(special_v_routes) + len(gap_hold_rows),
        "carrier_fixes": carrier_fixes,
        "expired_audit": expired_audit,
        "missing_airports_in_vamsys": sorted({a for g in plan_groups.values() for a in (g["orig"], g["dest"]) if a not in v_airports}),
        "alias_routes_in_vamsys": [],
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("=== REBUILD COMPLETE ===")
    for k, v in summary.items():
        if k not in ("expired_audit", "alias_routes_in_vamsys", "rotation_gaps"):
            print(f"  {k}: {v}")
    print("  expired_audit:", summary["expired_audit"])
    print("  alias_routes_in_vamsys:", summary["alias_routes_in_vamsys"])
    print(f"  Excel -> {OUT_XLSX}")
    print(f"  JSON  -> {OUT_JSON}")


if __name__ == "__main__":
    main()
