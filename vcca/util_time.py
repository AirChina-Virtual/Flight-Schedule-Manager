"""Time / weekday helpers shared by every layer.

Conventions (used everywhere in the package):
  * minutes-of-day: int 0..1439
  * "HH:MM" strings: zero-padded, always 5 chars ("" for unknown)
  * weekdays: int 0=Monday .. 6=Sunday (Python ``datetime.weekday()``), sets are ``frozenset[int]``
  * VAMSYS days string: "monday,tuesday,..." lowercase, comma separated, Monday-first order
  * CAAC FD3 / "fd3" bitmask: 7 chars, position i holds str(i+1) when weekday i is set, else "."
    e.g. {0, 2, 4, 6} <-> "1.3.5.7"
  * timestamps: int UNIX epoch seconds (UTC)
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable

UTC = _dt.timezone.utc

DAY_NAMES: tuple[str, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_TO_IDX: dict[str, int] = {d: i for i, d in enumerate(DAY_NAMES)}
DAY_ABBR_ZH: tuple[str, ...] = ("一", "二", "三", "四", "五", "六", "日")

MIN_PER_DAY = 1440
WEEK_S = 7 * 86400


# ---------------------------------------------------------------- HH:MM ----------------------------------------------
def parse_hhmm(val) -> int | None:
    """'HH:MM', 'HHMM', 'HMM', 'H:MM' -> minutes of day (no modulo). Invalid / empty -> None.

    Identical to legacy ``rebuild_vamsys_plan.parse_hhmm`` (e.g. '2400' -> 1440 is returned as-is).
    """
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


def fmt_hm(m: int | None) -> str:
    """minutes -> 'HH:MM' (mod 1440); None -> ''."""
    if m is None:
        return ""
    m = m % MIN_PER_DAY
    return f"{m // 60:02d}:{m % 60:02d}"


def norm_hhmm(val) -> str:
    """Normalise any parseable time ('945', '09:45:00' not supported -> '') to 'HH:MM'."""
    s = str(val or "").strip()
    if s.count(":") == 2:  # 'HH:MM:SS' from VAMSYS RawApiJson
        s = s.rsplit(":", 1)[0]
    return fmt_hm(parse_hhmm(s))


def circ_diff(m1: int, m2: int) -> int:
    """Signed circular difference m1 - m2 in minutes, range [-720, 719]."""
    return ((m1 - m2 + 720) % MIN_PER_DAY) - 720


def abs_circ_diff(m1: int, m2: int) -> int:
    return abs(circ_diff(m1, m2))


def hm_circ_diff(hm1: str, hm2: str) -> int:
    """Signed circular difference between two 'HH:MM' strings (raises TypeError if either is invalid)."""
    a, b = parse_hhmm(hm1), parse_hhmm(hm2)
    if a is None or b is None:
        raise TypeError(f"invalid HH:MM: {hm1!r} / {hm2!r}")
    return circ_diff(a, b)


def duration_min(dep_min: int, arr_min: int) -> int:
    """Block time assuming arrival is the next occurrence after departure (0..1439)."""
    return (arr_min - dep_min) % MIN_PER_DAY


# ---------------------------------------------------------------- weekday sets ---------------------------------------
def fd3_to_days(fd3: str) -> frozenset[int]:
    """CAAC FD3 '1.3.5.7' / '......7' -> weekday set. Any digit at position i sets weekday i (legacy behaviour)."""
    return frozenset(i for i, ch in enumerate(str(fd3 or "").strip()) if i < 7 and ch.isdigit())


def days_to_fd3(days: Iterable[int]) -> str:
    ds = set(days)
    return "".join(str(i + 1) if i in ds else "." for i in range(7))


def vdays_to_days(vdays: str) -> frozenset[int]:
    """VAMSYS 'monday,wednesday' -> {0, 2}. Unknown tokens are ignored."""
    out = set()
    for part in str(vdays or "").lower().split(","):
        p = part.strip()
        if p in DAY_TO_IDX:
            out.add(DAY_TO_IDX[p])
    return frozenset(out)


def days_to_vdays(days: Iterable[int]) -> str:
    return ",".join(DAY_NAMES[i] for i in sorted(set(days)))


def days_to_zh(days: Iterable[int]) -> str:
    """{0, 2} -> '一三' (report helper)."""
    return "".join(DAY_ABBR_ZH[i] for i in sorted(set(days)))


def shift_days(days: Iterable[int], shift: int) -> frozenset[int]:
    """Shift every weekday by ``shift`` days (mod 7)."""
    return frozenset((d + shift) % 7 for d in days)


# ---------------------------------------------------------------- local <-> UTC --------------------------------------
def local_to_utc(local_min: int, tz_offset_min: int = 480, day_offset: int = 0) -> tuple[int, int]:
    """Convert a local (e.g. BJT) clock time to UTC.

    ``day_offset`` is the number of whole days the local time lies after the reference (operating) day,
    e.g. leg 2 of a CAAC row departing after midnight has day_offset=1.
    Returns (utc_minutes_of_day, day_shift) where day_shift is the weekday shift to apply to the
    operating-day set (-1 when BJT dep < 08:00 and day_offset=0, +0/+1 ... for later legs).
    """
    utc_abs = day_offset * MIN_PER_DAY + local_min - tz_offset_min
    return utc_abs % MIN_PER_DAY, utc_abs // MIN_PER_DAY


def local_days_to_utc(days: Iterable[int], local_min: int, tz_offset_min: int = 480, day_offset: int = 0) -> frozenset[int]:
    """Operating-day weekday set -> UTC departure weekday set for a leg departing at ``local_min`` (+day_offset)."""
    _, shift = local_to_utc(local_min, tz_offset_min, day_offset)
    return shift_days(days, shift)


# ---------------------------------------------------------------- timestamps -----------------------------------------
def ts_to_dt(ts) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(int(ts), tz=UTC)


def ts_utc_weekday(ts) -> int:
    return ts_to_dt(ts).weekday()


def ts_utc_hm(ts) -> str:
    return "" if not ts else ts_to_dt(ts).strftime("%H:%M")


def ts_utc_min(ts) -> int:
    d = ts_to_dt(ts)
    return d.hour * 60 + d.minute


def ts_utc_date(ts) -> str:
    return "" if not ts else ts_to_dt(ts).strftime("%Y-%m-%d")


def ts_utc_str(ts) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC) or ''."""
    return "" if not ts else ts_to_dt(ts).strftime("%Y-%m-%d %H:%M:%S")


def round_ts(ts: int, minutes: int = 5) -> int:
    """Round to nearest ``minutes`` (legacy real-time fallback: ((t + 150) // 300) * 300)."""
    step = minutes * 60
    return ((int(ts) + step // 2) // step) * step


def local_midnight_utc_ts(date: _dt.date, tz_offset_min: int = 480) -> int:
    """UTC epoch of 00:00 local time on ``date`` (e.g. 2026-10-25 BJT -> 2026-10-24 16:00Z)."""
    dt = _dt.datetime(date.year, date.month, date.day, tzinfo=UTC) - _dt.timedelta(minutes=tz_offset_min)
    return int(dt.timestamp())


def next_local_midnight_utc(now: _dt.datetime | None = None, tz_offset_min: int = 480) -> _dt.datetime:
    """The next 00:00 local time strictly after ``now`` (aware UTC datetime; default: current time), as UTC."""
    now = now or _dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(_dt.timezone(_dt.timedelta(minutes=tz_offset_min)))
    next_date = local.date() + _dt.timedelta(days=1)
    return _dt.datetime.fromtimestamp(local_midnight_utc_ts(next_date, tz_offset_min), tz=UTC)


# ---------------------------------------------------------------- weeks ----------------------------------------------
def monday_anchor_ts(ts: int) -> int:
    """UTC epoch of Monday 00:00Z of the week containing ``ts``."""
    d = ts_to_dt(ts)
    monday = _dt.datetime(d.year, d.month, d.day, tzinfo=UTC) - _dt.timedelta(days=d.weekday())
    return int(monday.timestamp())


def week_index(ts: int, anchor_ts: int) -> int:
    """Monday-aligned (UTC) week number of ``ts`` relative to the week containing ``anchor_ts`` (0 = same week,
    negative = earlier). Used to count "distinct weeks" for the weekday add/drop rules."""
    return (monday_anchor_ts(ts) - monday_anchor_ts(anchor_ts)) // WEEK_S


def iso_week_key(ts: int) -> tuple[int, int]:
    y, w, _ = ts_to_dt(ts).isocalendar()
    return y, w
