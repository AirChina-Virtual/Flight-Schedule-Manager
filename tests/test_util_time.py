import datetime as dt
import unittest

from tests import _support  # noqa: F401  (sys.path)
from vcca import util_time as ut
from vcca.caac import parse_row_legs

UTC = dt.timezone.utc
OPS = ("CCA", "CAO", "CCD", "BJN")


def row(**kw):
    base = {"FD1": "CCA1001", "FD2": "738", "FD3": "1......", "FD4": "ZBAA", "FD5": "", "FD6": "", "FD7": "ZSSS",
            "FD8": "", "FD9": "", "FD10": "", "FD19": "", "FD23": "1025", "FD24": "0327", "FD26": "J"}
    base.update(kw)
    return base


class HhmmTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ut.parse_hhmm("0905"), 545)
        self.assertEqual(ut.parse_hhmm("905"), 545)
        self.assertEqual(ut.parse_hhmm("09:05"), 545)
        self.assertEqual(ut.parse_hhmm(930), 570)
        self.assertIsNone(ut.parse_hhmm(""))
        self.assertIsNone(ut.parse_hhmm("ab:cd"))
        self.assertIsNone(ut.parse_hhmm("12345"))

    def test_fmt(self):
        self.assertEqual(ut.fmt_hm(545), "09:05")
        self.assertEqual(ut.fmt_hm(-60), "23:00")
        self.assertEqual(ut.fmt_hm(1440 + 5), "00:05")
        self.assertEqual(ut.fmt_hm(None), "")
        self.assertEqual(ut.norm_hhmm("15:50:00"), "15:50")

    def test_circular(self):
        self.assertEqual(ut.circ_diff(10, 1430), 20)
        self.assertEqual(ut.circ_diff(1430, 10), -20)
        self.assertEqual(ut.hm_circ_diff("00:10", "23:50"), 20)
        self.assertEqual(ut.abs_circ_diff(0, 720), 720)
        self.assertEqual(ut.duration_min(23 * 60, 60), 120)


class WeekdayTest(unittest.TestCase):
    def test_fd3(self):
        self.assertEqual(ut.fd3_to_days("1.3.5.7"), frozenset({0, 2, 4, 6}))
        self.assertEqual(ut.fd3_to_days("......7"), frozenset({6}))
        self.assertEqual(ut.days_to_fd3({0, 2, 4, 6}), "1.3.5.7")
        self.assertEqual(ut.days_to_fd3(set()), ".......")

    def test_vdays(self):
        s = "monday,wednesday,sunday"
        self.assertEqual(ut.vdays_to_days(s), frozenset({0, 2, 6}))
        self.assertEqual(ut.days_to_vdays({6, 0, 2}), s)
        self.assertEqual(ut.vdays_to_days("Monday, bogus ,TUESDAY"), frozenset({0, 1}))

    def test_roundtrip_all_masks(self):
        for mask in range(128):
            days = frozenset(i for i in range(7) if mask >> i & 1)
            self.assertEqual(ut.fd3_to_days(ut.days_to_fd3(days)), days)
            self.assertEqual(ut.vdays_to_days(ut.days_to_vdays(days)), days)

    def test_shift(self):
        self.assertEqual(ut.shift_days({0, 6}, -1), frozenset({6, 5}))
        self.assertEqual(ut.shift_days({6}, 1), frozenset({0}))


class LocalUtcTest(unittest.TestCase):
    def test_bjt_after_0800_same_day(self):
        self.assertEqual(ut.local_to_utc(10 * 60 + 40, 480), (160, 0))

    def test_bjt_before_0800_previous_day(self):
        self.assertEqual(ut.local_to_utc(7 * 60 + 30, 480), (23 * 60 + 30, -1))
        self.assertEqual(ut.local_days_to_utc({0}, 7 * 60 + 30, 480), frozenset({6}))  # Mon BJT -> Sun UTC

    def test_day_offset(self):
        self.assertEqual(ut.local_to_utc(3 * 60, 480, day_offset=1), (19 * 60, 0))
        self.assertEqual(ut.local_to_utc(9 * 60, 480, day_offset=1), (60, 1))
        self.assertEqual(ut.local_to_utc(60, 480, day_offset=2), (17 * 60, 1))


class LegExpansionTest(unittest.TestCase):
    """CAAC row -> legs: BJT-8h, weekday shift, cumulative day offset for leg 2."""

    def test_leg1_pre_0800_weekday_shift(self):
        (leg,) = parse_row_legs(row(FD3="1.3....", FD5="0630", FD6="0900"), target_ops=OPS)
        self.assertEqual((leg.utc_dep, leg.utc_arr), ("22:30", "01:00"))
        self.assertEqual(leg.utc_days, frozenset({6, 1}))  # Mon/Wed BJT -> Sun/Tue UTC
        self.assertEqual(leg.day_offset, 0)

    def test_leg1_after_0800_no_shift(self):
        (leg,) = parse_row_legs(row(FD3="1.3....", FD5="0800", FD6="1000"), target_ops=OPS)
        self.assertEqual(leg.utc_dep, "00:00")
        self.assertEqual(leg.utc_days, frozenset({0, 2}))

    def test_leg2_offset_after_overnight_leg1(self):
        # leg1 23:00 -> 01:30 (+1 day), leg2 03:00 -> 05:00: leg2 departs on operating day +1
        l1, l2 = parse_row_legs(row(FD5="2300", FD6="0130", FD8="0300", FD9="0500", FD10="RJAA"), target_ops=OPS)
        self.assertEqual((l1.utc_dep, l1.utc_days), ("15:00", frozenset({0})))
        self.assertEqual(l2.day_offset, 1)
        self.assertEqual((l2.orig, l2.dest, l2.utc_dep, l2.utc_arr), ("ZSSS", "RJAA", "19:00", "21:00"))
        self.assertEqual(l2.utc_days, frozenset({0}))  # Mon +1d BJT 03:00 = Mon 19:00Z
        self.assertEqual(l2.bjt_days_label, "1......(+1d)")
        self.assertTrue(l1.is_multileg and l2.is_multileg)

    def test_leg2_offset_counts_turnaround_past_midnight(self):
        # leg1 20:00 -> 22:30, leg2 departs 09:30 (< 22:30) -> next day
        l1, l2 = parse_row_legs(row(FD3="......7", FD5="2000", FD6="2230", FD8="0930", FD9="1200", FD10="ZGGG"),
                                target_ops=OPS)
        self.assertEqual(l2.day_offset, 1)
        self.assertEqual(l2.utc_dep, "01:30")
        self.assertEqual(l2.utc_days, frozenset({0}))  # Sun +1 = Mon BJT 09:30 = Mon 01:30Z

    def test_leg2_double_offset(self):
        # leg1 22:00 -> 02:00 (+1), leg2 01:00 < 02:00 (+1 more) -> day_offset 2
        _, l2 = parse_row_legs(row(FD5="2200", FD6="0200", FD8="0100", FD9="0400", FD10="ZGGG"), target_ops=OPS)
        self.assertEqual(l2.day_offset, 2)
        self.assertEqual(l2.utc_dep, "17:00")
        self.assertEqual(l2.utc_days, frozenset({1}))  # Mon +2d 01:00 BJT = Tue 17:00Z

    def test_leg2_pre_0800_with_offset_zero(self):
        # leg2 BJT 07:00 same day (leg1 05:00->06:30) -> previous UTC day
        _, l2 = parse_row_legs(row(FD3=".2.....", FD5="0500", FD6="0630", FD8="0700", FD9="0900", FD10="ZGGG"),
                               target_ops=OPS)
        self.assertEqual(l2.day_offset, 0)
        self.assertEqual((l2.utc_dep, l2.utc_days), ("23:00", frozenset({0})))

    def test_non_target_and_cargo(self):
        self.assertEqual(parse_row_legs(row(FD1="CES5101", FD5="1000", FD6="1200"), target_ops=OPS), [])
        (leg,) = parse_row_legs(row(FD1="CAO1001", FD5="1000", FD6="1200"), target_ops=OPS)
        self.assertEqual((leg.route_type, leg.number), ("cargo", "CA1001"))
        (leg,) = parse_row_legs(row(FD26="F", FD5="1000", FD6="1200"), target_ops=OPS)
        self.assertEqual(leg.route_type, "cargo")
        (leg,) = parse_row_legs(row(FD23="1025", FD24="1025", FD5="1000", FD6="1200"), target_ops=OPS)
        self.assertTrue(leg.single_day)


class TimestampTest(unittest.TestCase):
    def test_ts_helpers(self):
        ts = int(dt.datetime(2026, 10, 24, 16, 0, tzinfo=UTC).timestamp())
        self.assertEqual(ut.local_midnight_utc_ts(dt.date(2026, 10, 25), 480), ts)
        self.assertEqual(ut.ts_utc_weekday(ts), 5)  # Saturday UTC
        self.assertEqual(ut.ts_utc_hm(ts), "16:00")
        self.assertEqual(ut.ts_utc_str(ts), "2026-10-24 16:00:00")
        self.assertEqual(ut.round_ts(ts + 149), ts)
        self.assertEqual(ut.round_ts(ts + 150), ts + 300)

    def test_next_local_midnight(self):
        f = ut.next_local_midnight_utc
        # 2026-11-25 10:00Z = 18:00 BJT -> next BJT midnight 2026-11-26 00:00 = 2026-11-25 16:00Z
        self.assertEqual(f(dt.datetime(2026, 11, 25, 10, 0, tzinfo=UTC)), dt.datetime(2026, 11, 25, 16, 0, tzinfo=UTC))
        # 2026-11-25 17:00Z = 01:00 BJT on the 26th -> 2026-11-26 16:00Z
        self.assertEqual(f(dt.datetime(2026, 11, 25, 17, 0, tzinfo=UTC)), dt.datetime(2026, 11, 26, 16, 0, tzinfo=UTC))
        # exactly midnight BJT -> the following one
        self.assertEqual(f(dt.datetime(2026, 11, 25, 16, 0, tzinfo=UTC)), dt.datetime(2026, 11, 26, 16, 0, tzinfo=UTC))

    def test_week_index(self):
        anchor = int(dt.datetime(2026, 10, 24, 16, 0, tzinfo=UTC).timestamp())  # Sat
        mon = int(dt.datetime(2026, 10, 26, 0, 0, tzinfo=UTC).timestamp())
        self.assertEqual(ut.week_index(anchor, anchor), 0)
        self.assertEqual(ut.week_index(mon - 1, anchor), 0)
        self.assertEqual(ut.week_index(mon, anchor), 1)
        self.assertEqual(ut.week_index(mon + 7 * 86400, anchor), 2)
        self.assertEqual(ut.week_index(anchor - 6 * 86400, anchor), -1)


if __name__ == "__main__":
    unittest.main()
