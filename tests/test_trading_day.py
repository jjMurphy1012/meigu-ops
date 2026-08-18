"""交易日历测试。

这些断言不是随便挑的日期 —— 覆盖的都是人算容易错的边界:
观察日顺延、耶稣受难日、半日市、长周末。
"""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from meigu_lib import (  # noqa: E402
    _easter,
    _nth_weekday,
    day_info,
    half_days,
    is_trading_day,
    market_holidays,
    next_trading_day,
    prev_trading_day,
    trading_days_between,
)


class TestNthWeekday(unittest.TestCase):
    def test_third_monday_january_2026(self):
        # MLK Day 2026
        self.assertEqual(_nth_weekday(2026, 1, 0, 3), dt.date(2026, 1, 19))

    def test_first_monday_september_2026(self):
        # Labor Day 2026
        self.assertEqual(_nth_weekday(2026, 9, 0, 1), dt.date(2026, 9, 7))

    def test_fourth_thursday_november_2026(self):
        # Thanksgiving 2026
        self.assertEqual(_nth_weekday(2026, 11, 3, 4), dt.date(2026, 11, 26))

    def test_last_monday_may_2026(self):
        # Memorial Day 2026 —— 倒数第一个周一
        self.assertEqual(_nth_weekday(2026, 5, 0, -1), dt.date(2026, 5, 25))

    def test_last_monday_may_2027(self):
        self.assertEqual(_nth_weekday(2027, 5, 0, -1), dt.date(2027, 5, 31))


class TestEaster(unittest.TestCase):
    def test_known_easters(self):
        self.assertEqual(_easter(2026), dt.date(2026, 4, 5))
        self.assertEqual(_easter(2027), dt.date(2027, 3, 28))
        self.assertEqual(_easter(2025), dt.date(2025, 4, 20))


class TestHolidays(unittest.TestCase):
    def test_2026_matches_known_closures(self):
        """对齐 2026 年公开的 NYSE 休市日。"""
        h = market_holidays(2026)
        expected = {
            dt.date(2026, 1, 1),    # 元旦(周四)
            dt.date(2026, 1, 19),   # MLK
            dt.date(2026, 2, 16),   # 华盛顿诞辰
            dt.date(2026, 4, 3),    # 耶稣受难日
            dt.date(2026, 5, 25),   # 阵亡将士
            dt.date(2026, 6, 19),   # 六月节(周五)
            dt.date(2026, 7, 3),    # 独立日观察日(7/4 是周六 → 提前到周五)
            dt.date(2026, 9, 7),    # 劳动节
            dt.date(2026, 11, 26),  # 感恩节
            dt.date(2026, 12, 25),  # 圣诞(周五)
        }
        self.assertEqual(set(h), expected)

    def test_observed_shifts_saturday_back(self):
        # 2026-07-04 是周六 → 观察日为 7/3(周五)
        h = market_holidays(2026)
        self.assertIn(dt.date(2026, 7, 3), h)
        self.assertNotIn(dt.date(2026, 7, 6), h)

    def test_observed_shifts_sunday_forward(self):
        # 2027-01-01 是周五,正常;测 2028-01-01(周六)→ 2027-12-31
        h = market_holidays(2028)
        self.assertIn(dt.date(2027, 12, 31), h)


class TestHalfDays(unittest.TestCase):
    def test_2026_half_days(self):
        hd = half_days(2026)
        self.assertIn(dt.date(2026, 11, 27), hd)  # 感恩节次日
        self.assertIn(dt.date(2026, 12, 24), hd)  # 平安夜(周四)

    def test_july_3_not_half_day_when_it_is_the_holiday(self):
        """2026 年 7/3 是独立日观察日(全天休市),不应同时被标成半日市。"""
        self.assertNotIn(dt.date(2026, 7, 3), half_days(2026))

    def test_half_day_close_time(self):
        info = day_info(dt.date(2026, 11, 27))
        self.assertTrue(info.is_trading_day)
        self.assertTrue(info.is_half_day)
        self.assertEqual(info.close_et, "13:00")

    def test_normal_day_close_time(self):
        info = day_info(dt.date(2026, 8, 17))  # 周一
        self.assertEqual(info.close_et, "16:00")

    def test_non_trading_day_has_no_close(self):
        self.assertIsNone(day_info(dt.date(2026, 12, 25)).close_et)


class TestTradingDay(unittest.TestCase):
    def test_weekend_is_not_trading_day(self):
        self.assertFalse(is_trading_day(dt.date(2026, 8, 15)))  # 周六
        self.assertFalse(is_trading_day(dt.date(2026, 8, 16)))  # 周日

    def test_holiday_is_not_trading_day(self):
        self.assertFalse(is_trading_day(dt.date(2026, 11, 26)))

    def test_half_day_is_still_trading_day(self):
        self.assertTrue(is_trading_day(dt.date(2026, 11, 27)))

    def test_prev_trading_day_skips_weekend(self):
        # 2026-08-17 周一 → 上一交易日是 08-14 周五
        self.assertEqual(prev_trading_day(dt.date(2026, 8, 17)), dt.date(2026, 8, 14))

    def test_prev_trading_day_skips_long_weekend(self):
        # 感恩节长周末:11/30 周一 → 上一交易日是 11/27 半日市周五
        self.assertEqual(prev_trading_day(dt.date(2026, 11, 30)), dt.date(2026, 11, 27))

    def test_next_trading_day_skips_holiday(self):
        # 11/25 周三 → 下一交易日跳过 11/26 感恩节,到 11/27
        self.assertEqual(next_trading_day(dt.date(2026, 11, 25)), dt.date(2026, 11, 27))

    def test_next_trading_day_across_july_4_weekend_2026(self):
        # 7/2 周四 → 7/3 休市、7/4-5 周末 → 下一交易日 7/6 周一
        self.assertEqual(next_trading_day(dt.date(2026, 7, 2)), dt.date(2026, 7, 6))


class TestTradingDaysBetween(unittest.TestCase):
    def test_same_day_is_zero(self):
        d = dt.date(2026, 8, 17)
        self.assertEqual(trading_days_between(d, d), 0)

    def test_consecutive_weekdays(self):
        # (8/17, 8/21] = 18,19,20,21 → 4 个交易日
        self.assertEqual(trading_days_between(dt.date(2026, 8, 17), dt.date(2026, 8, 21)), 4)

    def test_excludes_weekend(self):
        # (8/14 周五, 8/17 周一] → 只有 8/17
        self.assertEqual(trading_days_between(dt.date(2026, 8, 14), dt.date(2026, 8, 17)), 1)

    def test_excludes_holiday(self):
        # (11/25, 11/30] → 11/26 感恩节休市、11/28-29 周末 → 11/27 + 11/30 = 2
        self.assertEqual(trading_days_between(dt.date(2026, 11, 25), dt.date(2026, 11, 30)), 2)

    def test_reversed_range_is_zero(self):
        self.assertEqual(trading_days_between(dt.date(2026, 8, 21), dt.date(2026, 8, 17)), 0)


if __name__ == "__main__":
    unittest.main()
