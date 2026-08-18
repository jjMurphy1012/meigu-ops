#!/usr/bin/env python3
"""交易日历查询。

用法:
    python3 scripts/trading_day.py                # 今天(ET)
    python3 scripts/trading_day.py 2026-11-27     # 指定日期
    python3 scripts/trading_day.py --year 2026    # 全年休市日与半日市
    python3 scripts/trading_day.py --json

为什么要脚本:长周末、观察日顺延、耶稣受难日、半日市这些规则,人算容易错,
而错一次的代价是基于错误假设跑一整天的决策流程。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from meigu_lib import (
    WEEKDAY_ZH,
    day_info,
    half_days,
    market_holidays,
    next_trading_day,
    prev_trading_day,
    today_et,
)


def describe(date: dt.date) -> dict:
    info = day_info(date)
    return {
        "date": date.isoformat(),
        "weekday": WEEKDAY_ZH[date.weekday()],
        "is_trading_day": info.is_trading_day,
        "is_half_day": info.is_half_day,
        "reason": info.reason,
        "close_et": info.close_et,
        "prev_trading_day": prev_trading_day(date).isoformat(),
        "next_trading_day": next_trading_day(date).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="美股交易日历查询")
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD,默认今天(ET)")
    ap.add_argument("--year", type=int, help="列出该年全部休市日与半日市")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    if args.year:
        holidays = market_holidays(args.year)
        halves = half_days(args.year)
        if args.json:
            print(
                json.dumps(
                    {
                        "year": args.year,
                        "holidays": {d.isoformat(): n for d, n in sorted(holidays.items())},
                        "half_days": {d.isoformat(): n for d, n in sorted(halves.items())},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"=== {args.year} 年美股休市日 ===")
        for d, name in sorted(holidays.items()):
            print(f"  {d}  {WEEKDAY_ZH[d.weekday()]}  {name}")
        print(f"\n=== {args.year} 年提前收盘日(13:00 ET)===")
        for d, name in sorted(halves.items()):
            print(f"  {d}  {WEEKDAY_ZH[d.weekday()]}  {name}")
        print("\n注:不含国葬/极端天气等临时休市。")
        return 0

    date = dt.date.fromisoformat(args.date) if args.date else today_et()
    result = describe(date)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["is_trading_day"] else 1

    mark = "✅ 交易日" if result["is_trading_day"] else "❌ 非交易日"
    print(f"{result['date']}({result['weekday']})  {mark}")
    print(f"  状态:{result['reason']}")
    if result["close_et"]:
        print(f"  收盘:{result['close_et']} ET")
    print(f"  上一交易日:{result['prev_trading_day']}")
    print(f"  下一交易日:{result['next_trading_day']}")
    return 0 if result["is_trading_day"] else 1


if __name__ == "__main__":
    sys.exit(main())
