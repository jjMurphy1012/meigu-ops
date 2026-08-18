#!/usr/bin/env python3
"""从模板生成当日收盘日报骨架。

用法:
    python3 scripts/new_report.py                 # 今天(ET)
    python3 scripts/new_report.py 2026-08-17
    python3 scripts/new_report.py --force         # 覆盖已存在的文件

产出 reports/YYYY-MM-DD.md(已 gitignore)。
关注股清单从 config/watchlist.toml 注入,不在模板里硬编码。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from meigu_lib import (
    REPORTS_DIR,
    TEMPLATES_DIR,
    WEEKDAY_ZH,
    day_info,
    load_config,
    now_et,
    prev_trading_day,
    today_et,
)


# ── 日报骨架全部由 config/watchlist.toml 驱动 ──
# 仓库不预设研究范围:哪些指数/板块/主题/分组/风险维度都来自用户配置。
# 缺某一节时给通用兜底,并在骨架里注明"未配置",而不是塞一份别人的偏好。

DEFAULT_INDICES = ["Dow Jones", "S&P 500", "Nasdaq Composite", "Russell 2000 / IWM", "VIX"]
DEFAULT_TECHNICALS = ["SPY", "QQQ", "IWM"]
DEFAULT_WATCH_LEVELS = ["SPY 支撑 / 压力", "QQQ 支撑 / 压力"]
DEFAULT_RISKS = ["宏观利率", "市场宽度", "估值与拥挤度", "财报风险",
                 "地缘风险", "技术面", "流动性"]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("-" * (len(h) + 2) for h in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _missing(what: str, key: str) -> str:
    return (f"> ⚠️ `config/watchlist.toml` 里没有配置{what}(`{key}`)。\n"
            f"> 先 `cp config/watchlist.example.toml config/watchlist.toml` 并按自己的研究方式填写。\n")


def build_indices_section(cfg: dict) -> str:
    rows = (cfg.get("report", {}) or {}).get("indices") or []
    names = [str(r.get("name", "")) for r in rows] or DEFAULT_INDICES
    return _table(["指数", "收盘点位", "涨跌幅", "日内高低点", "成交量变化", "技术状态"],
                  [[n, "", "", "", "", ""] for n in names])


def build_sectors_section(cfg: dict) -> str:
    rows = (cfg.get("report", {}) or {}).get("sectors") or []
    if not rows:
        return _missing("板块", "report.sectors")
    return _table(["排名", "板块", "ETF", "当日", "近5日", "近1月", "vs 基准", "主要驱动"],
                  [["", str(r.get("name", "")), str(r.get("etf", "")), "", "", "", "", ""]
                   for r in rows])


def build_themes_section(cfg: dict) -> str:
    rows = (cfg.get("report", {}) or {}).get("themes") or []
    if not rows:
        return _missing("主题与风格", "report.themes")
    return _table(["主题 / 风格", "代表", "当日", "近5日", "近1月", "解读"],
                  [[str(r.get("name", "")), str(r.get("proxy", "")), "", "", "", ""]
                   for r in rows])


def build_technicals_section(cfg: dict) -> str:
    syms = (cfg.get("report", {}) or {}).get("technicals") or DEFAULT_TECHNICALS
    return _table(["标的", "现价", "20日", "50日", "100日", "200日", "RSI",
                   "MACD/趋势", "关键支撑", "关键压力"],
                  [[s] + [""] * 9 for s in syms])


def build_group_news_section(cfg: dict) -> str:
    groups = cfg.get("groups") or []
    if not groups:
        return _missing("关注分组", "[[groups]]")
    blocks = []
    for i, g in enumerate(groups, 1):
        blocks.append(f"### 8.{i} {g.get('name', '未命名分组')}\n")
        blocks.append(_table(["股票", "涨跌幅", "原因", "技术位置", "后续关注"],
                             [[s, "", "", "", ""] for s in g.get("symbols", [])]))
        blocks.append("")
    blocks.append(f"### 8.{len(groups) + 1} 其他显著异动\n")
    blocks.append("{财报后大涨大跌 / 盘后异动 / 评级调整 / 并购 / 监管调查 / "
                  "管理层变动 / 回购增发 / 空头报告 / 指引变化}")
    return "\n".join(blocks)


def build_watchlist_section(cfg: dict) -> str:
    groups = cfg.get("groups") or []
    if not groups:
        return _missing("关注分组", "[[groups]]")
    blocks = []
    for g in groups:
        blocks.append(f"**{g.get('name', '未命名分组')}**\n")
        blocks.append(_table(["标的", "当日涨跌", "当前趋势", "关键新闻", "支撑", "压力", "判断"],
                             [[s, "", "", "", "", "", ""] for s in g.get("symbols", [])]))
        blocks.append("")
    return "\n".join(blocks)


def build_watch_levels_section(cfg: dict) -> str:
    items = (cfg.get("report", {}) or {}).get("watch_levels") or DEFAULT_WATCH_LEVELS
    return "\n".join(f"- {x}:" for x in items)


def build_risk_section(cfg: dict) -> str:
    dims = (cfg.get("report", {}) or {}).get("risk_dimensions") or DEFAULT_RISKS
    return _table(["风险维度", "当前状态", "风险等级"], [[d, "", ""] for d in dims])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成收盘日报骨架")
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD,默认今天(ET)")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的日报")
    ap.add_argument("--allow-non-trading", action="store_true", help="非交易日也生成")
    args = ap.parse_args(argv)

    date = dt.date.fromisoformat(args.date) if args.date else today_et()
    info = day_info(date)

    if not info.is_trading_day and not args.allow_non_trading:
        print(f"❌ {date}({WEEKDAY_ZH[date.weekday()]})不是交易日 —— {info.reason}")
        print("   若确实要生成,加 --allow-non-trading。")
        return 1

    template_path = TEMPLATES_DIR / "daily-report.md"
    if not template_path.exists():
        print(f"❌ 找不到模板 {template_path}")
        return 1

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{date.isoformat()}.md"
    if out_path.exists() and not args.force:
        print(f"⚠️  {out_path.relative_to(REPORTS_DIR.parent)} 已存在。加 --force 覆盖。")
        return 1

    # watchlist 用 required=False:没有真配置时退回样例,只提示不阻断
    watchlist = load_config("watchlist", required=False)
    if watchlist.get("_is_example"):
        print("⚠️  用的是 watchlist.example.toml —— 建议复制成 watchlist.toml 后按自己的主线修改。")

    body = template_path.read_text(encoding="utf-8")
    body = body.replace("{{DATE}}", date.isoformat())
    body = body.replace(
        "{{PREV_DATE}}",
        f"{prev_trading_day(date).isoformat()}({WEEKDAY_ZH[prev_trading_day(date).weekday()]})",
    )
    body = body.replace("{{GENERATED_AT}}", now_et().strftime("%Y-%m-%d %H:%M ET"))
    for token, builder in (
        ("{{INDICES_SECTION}}", build_indices_section),
        ("{{SECTORS_SECTION}}", build_sectors_section),
        ("{{THEMES_SECTION}}", build_themes_section),
        ("{{TECHNICALS_SECTION}}", build_technicals_section),
        ("{{GROUP_NEWS_SECTION}}", build_group_news_section),
        ("{{WATCHLIST_SECTION}}", build_watchlist_section),
        ("{{WATCH_LEVELS_SECTION}}", build_watch_levels_section),
        ("{{RISK_SECTION}}", build_risk_section),
    ):
        body = body.replace(token, builder(watchlist))

    if info.is_half_day:
        body = body.replace(
            f"# 美股收盘日报丨{date.isoformat()}",
            f"# 美股收盘日报丨{date.isoformat()}\n\n> ⏰ **半日市**:{info.reason}",
        )

    out_path.write_text(body, encoding="utf-8")

    print(f"✅ 已生成 reports/{out_path.name}")
    print(f"   交易日:{info.reason} · 收盘 {info.close_et} ET")
    print(f"   上一交易日:{prev_trading_day(date)}")
    print("   下一步:按 modes/daily.md 逐节填写。取不到的数据写「暂无可靠数据」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
