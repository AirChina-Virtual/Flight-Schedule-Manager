"""Pure-Python OLE2/BIFF8 .xls reader (copied verbatim from fr24_work/biff8_xls.py; no third-party deps)."""

import struct
from pathlib import Path


def extract_cfb_stream(data: bytes, target_name: str = "Workbook") -> bytes:
    if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("Not an OLE2 Compound File")
    sec_shift = struct.unpack_from("<H", data, 30)[0]
    mini_sec_shift = struct.unpack_from("<H", data, 32)[0]
    sec_size = 1 << sec_shift
    mini_sec_size = 1 << mini_sec_shift
    num_fat_secs = struct.unpack_from("<I", data, 44)[0]
    first_dir_sec = struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    first_mini_fat_sec = struct.unpack_from("<i", data, 60)[0]
    num_mini_fat_secs = struct.unpack_from("<I", data, 64)[0]
    first_difat_sec = struct.unpack_from("<i", data, 68)[0]
    num_difat_secs = struct.unpack_from("<I", data, 72)[0]
    difat_inline = list(struct.unpack_from("<109I", data, 76))

    def sec_off(sec_id: int) -> int:
        return (sec_id + 1) * sec_size

    fat_secs = [s for s in difat_inline if s < 0xFFFFFFFA][:num_fat_secs]
    ds = first_difat_sec
    for _ in range(num_difat_secs):
        if ds < 0 or ds >= 0xFFFFFFFA:
            break
        blk = data[sec_off(ds):sec_off(ds) + sec_size]
        entries = struct.unpack_from(f"<{sec_size // 4}I", blk, 0)
        fat_secs.extend(s for s in entries[:-1] if s < 0xFFFFFFFA)
        ds = entries[-1] if entries[-1] < 0xFFFFFFFA else -1
    fat_secs = fat_secs[:num_fat_secs]

    fat = []
    for fs in fat_secs:
        blk = data[sec_off(fs):sec_off(fs) + sec_size]
        fat.extend(struct.unpack_from(f"<{sec_size // 4}I", blk, 0))

    def read_chain(start_sec: int, max_len: int | None = None) -> bytes:
        out = bytearray()
        s = start_sec
        seen = set()
        while 0 <= s < 0xFFFFFFFA and s not in seen:
            seen.add(s)
            off = sec_off(s)
            out.extend(data[off:off + sec_size])
            s = fat[s] if s < len(fat) else 0xFFFFFFFE
            if max_len is not None and len(out) >= max_len:
                break
        return bytes(out[:max_len] if max_len is not None else out)

    dir_bytes = read_chain(first_dir_sec)
    entries = []
    for i in range(0, len(dir_bytes), 128):
        ent = dir_bytes[i:i + 128]
        if len(ent) < 128:
            break
        nlen = struct.unpack_from("<H", ent, 64)[0]
        obj_type = ent[66]
        start_sec = struct.unpack_from("<I", ent, 116)[0]
        stream_size = struct.unpack_from("<Q", ent, 120)[0] & 0xFFFFFFFF
        name = ent[:max(0, nlen - 2)].decode("utf-16le", "replace") if nlen >= 2 else ""
        entries.append((name, obj_type, start_sec, stream_size))

    root = entries[0]
    root_stream = read_chain(root[2], root[3]) if root[3] > 0 else b""
    mini_fat = []
    if 0 <= first_mini_fat_sec < 0xFFFFFFFA and num_mini_fat_secs > 0:
        mf_bytes = read_chain(first_mini_fat_sec)
        mini_fat = list(struct.unpack_from(f"<{len(mf_bytes) // 4}I", mf_bytes, 0))

    for name, obj_type, ssec, ssz in entries:
        if obj_type == 2 and name.lower() == target_name.lower():
            if ssz < mini_cutoff and root_stream:
                out = bytearray()
                s = ssec
                seen = set()
                while 0 <= s < 0xFFFFFFFA and s not in seen:
                    seen.add(s)
                    off = s * mini_sec_size
                    out.extend(root_stream[off:off + mini_sec_size])
                    s = mini_fat[s] if s < len(mini_fat) else 0xFFFFFFFE
                    if len(out) >= ssz:
                        break
                return bytes(out[:ssz])
            return read_chain(ssec, ssz)
    raise KeyError(f"Stream {target_name} not found")


def _decode_rk(rk_val: int):
    is_int = bool(rk_val & 2)
    div100 = bool(rk_val & 1)
    if is_int:
        v = struct.unpack("<i", struct.pack("<I", rk_val & 0xFFFFFFFC))[0] >> 2
    else:
        v = struct.unpack("<d", struct.pack("<II", 0, rk_val & 0xFFFFFFFC))[0]
    return v / 100.0 if div100 else v


def read_biff8_sheets(xls_path: str | Path) -> dict[str, list[list]]:
    wb = extract_cfb_stream(Path(xls_path).read_bytes(), "Workbook")
    sheets_meta = []
    sst = []
    i = 0
    recs = []
    while i + 4 <= len(wb):
        rtype, rlen = struct.unpack_from("<HH", wb, i)
        recs.append((rtype, wb[i + 4:i + 4 + rlen]))
        i += 4 + rlen

    idx = 0
    while idx < len(recs):
        rtype, payload = recs[idx]
        if rtype == 0x0085:  # BOUNDSHEET
            bof_off = struct.unpack_from("<I", payload, 0)[0]
            cch, flags = payload[6], payload[7]
            sname = payload[8:8 + (2 * cch if flags & 1 else cch)].decode(
                "utf-16le" if flags & 1 else "latin1", "replace"
            )
            sheets_meta.append((sname, bof_off))
        elif rtype == 0x00FC:  # SST
            chunks = [payload]
            j = idx + 1
            while j < len(recs) and recs[j][0] == 0x003C:
                chunks.append(recs[j][1])
                j += 1
            ci, co = 0, 8
            _, uniq_strs = struct.unpack_from("<II", chunks[0], 0)

            def read_bytes(n):
                nonlocal ci, co
                out = bytearray()
                while n > 0:
                    avail = len(chunks[ci]) - co
                    if avail <= 0:
                        ci += 1
                        co = 0
                        avail = len(chunks[ci])
                    take = min(n, avail)
                    out.extend(chunks[ci][co:co + take])
                    co += take
                    n -= take
                return bytes(out)

            def read_biff_str():
                nonlocal ci, co
                if co >= len(chunks[ci]):
                    ci += 1
                    co = 0
                cch = struct.unpack_from("<H", read_bytes(2), 0)[0]
                flags = read_bytes(1)[0]
                is_uni = bool(flags & 0x01)
                has_ext = bool(flags & 0x04)
                has_rich = bool(flags & 0x08)
                rt_runs = struct.unpack_from("<H", read_bytes(2), 0)[0] if has_rich else 0
                ext_sz = struct.unpack_from("<I", read_bytes(4), 0)[0] if has_ext else 0
                chars = []
                rem = cch
                while rem > 0:
                    if co >= len(chunks[ci]):
                        ci += 1
                        cont_flag = chunks[ci][0]
                        co = 1
                        is_uni = bool(cont_flag & 0x01)
                    bpc = 2 if is_uni else 1
                    avail_chars = (len(chunks[ci]) - co) // bpc
                    take_chars = min(rem, avail_chars)
                    raw_c = chunks[ci][co:co + take_chars * bpc]
                    co += take_chars * bpc
                    chars.append(raw_c.decode("utf-16le" if is_uni else "latin1", "replace"))
                    rem -= take_chars
                if rt_runs:
                    read_bytes(4 * rt_runs)
                if ext_sz:
                    read_bytes(ext_sz)
                return "".join(chars)

            for _ in range(uniq_strs):
                sst.append(read_biff_str())
        idx += 1

    result = {}
    for sname, start_off in sheets_meta:
        rows = {}
        p = start_off
        while p + 4 <= len(wb):
            rt, rl = struct.unpack_from("<HH", wb, p)
            pl = wb[p + 4:p + 4 + rl]
            p += 4 + rl
            if rt == 0x000A:
                break
            elif rt == 0x00FD:
                r, c, _, sidx = struct.unpack_from("<HHHI", pl, 0)
                rows.setdefault(r, {})[c] = sst[sidx]
            elif rt == 0x0204:
                r, c, _ = struct.unpack_from("<HHH", pl, 0)
                slen = struct.unpack_from("<H", pl, 6)[0]
                uflag = pl[8]
                rows.setdefault(r, {})[c] = pl[9:9 + (2 * slen if uflag & 1 else slen)].decode(
                    "utf-16le" if uflag & 1 else "gb18030", "replace"
                )
            elif rt == 0x027E:
                r, c, _, rk = struct.unpack_from("<HHHI", pl, 0)
                rows.setdefault(r, {})[c] = _decode_rk(rk)
            elif rt == 0x00BD:
                r, c1 = struct.unpack_from("<HH", pl, 0)
                c2 = struct.unpack_from("<H", pl, len(pl) - 2)[0]
                for ci in range(c2 - c1 + 1):
                    _, rk = struct.unpack_from("<HI", pl, 4 + 6 * ci)
                    rows.setdefault(r, {})[c1 + ci] = _decode_rk(rk)
            elif rt == 0x0203:
                r, c, _, val = struct.unpack_from("<HHHd", pl, 0)
                rows.setdefault(r, {})[c] = val
            elif rt == 0x0006:
                r, c = struct.unpack_from("<HH", pl, 0)
                if pl[12:14] != b"\xff\xff":
                    rows.setdefault(r, {})[c] = struct.unpack_from("<d", pl, 6)[0]
        if not rows:
            result[sname] = []
            continue
        max_r = max(rows)
        max_c = max(max(cols) for cols in rows.values())
        result[sname] = [[rows.get(r, {}).get(c, "") for c in range(max_c + 1)] for r in range(max_r + 1)]
    return result
