import os, json, sqlite3
import openpyxl
from fr24_paths import PREP_OUTPUT_DIR, WORKSPACES_DB, CACHE_DIR

base_dir = PREP_OUTPUT_DIR
out_path = os.path.join(CACHE_DIR, "plan_catalog.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)


def read_sheet(wb, name, hdr_row=4):
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h).strip() if h is not None else f"C{i}" for i, h in enumerate(rows[hdr_row - 1])]
    res = []
    for idx, r in enumerate(rows[hdr_row:], start=hdr_row + 1):
        if any(c is not None for c in r):
            d = {hdr[i]: (str(r[i]) if hasattr(r[i], "strftime") else r[i]) for i in range(min(len(hdr), len(r)))}
            d["_row"] = idx
            res.append(d)
    return res


def main():
    wb2 = openpyxl.load_workbook(os.path.join(base_dir, "2026-2027冬春航季_VAMSYS新增调整拟停用清单.xlsx"), read_only=True, data_only=True)
    add_rows = read_sheet(wb2, "新增", 4)
    adj_rows = read_sheet(wb2, "调整", 4)
    stop_rows = read_sheet(wb2, "拟停用", 4)
    seg_rows = read_sheet(wb2, "冬春航段", 4)
    cur_rows = read_sheet(wb2, "现有航线", 4)
    wb2.close()

    iata_to_icao = {}
    icao_to_iata = {}
    conn = sqlite3.connect(f"file:{WORKSPACES_DB}?mode=ro", uri=True)
    for (rjson,) in conn.execute("SELECT json FROM resource_rows WHERE workspace_id='b8d41af1-5641-4b06-a476-b39829a63d25' AND resource=0 AND collection=0").fetchall():
        obj = json.loads(rjson)
        raw = json.loads(obj["RawApiJson"])
        icao = (raw.get("icao") or "").upper()
        iata = (raw.get("iata") or "").upper()
        if icao and iata:
            iata_to_icao[iata] = icao
            icao_to_iata[icao] = iata
    conn.close()

    wb3 = openpyxl.load_workbook(os.path.join(base_dir, "2026-2027冬春航季_实飞航迹与计划核对.xlsx"), read_only=True, data_only=True)
    rev_rows = read_sheet(wb3, "拟停用复核", 4)
    cmp_rows = read_sheet(wb3, "计划对照", 4)
    wb3.close()

    with open(os.path.join(base_dir, "2026-2027冬春航季_优先实飞核对队列.json"), "r", encoding="utf-8") as f:
        prio = json.load(f)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "iata_to_icao": iata_to_icao,
            "icao_to_iata": icao_to_iata,
            "counts": {
                "add": len(add_rows),
                "adj": len(adj_rows),
                "stop": len(stop_rows),
                "seg": len(seg_rows),
                "cur": len(cur_rows),
                "rev": len(rev_rows),
                "cmp": len(cmp_rows),
                "prio": len(prio["groups"]),
            },
            "add": add_rows,
            "adj": adj_rows,
            "stop": stop_rows,
            "seg": seg_rows,
            "cur": cur_rows,
            "rev": rev_rows,
            "cmp": cmp_rows,
            "prio": prio["groups"],
        }, f, ensure_ascii=False)

    print("Saved plan_catalog.json! iata_to_icao size:", len(iata_to_icao))


if __name__ == "__main__":
    main()
