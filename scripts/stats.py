#!/usr/bin/env python3
"""交易台账绩效统计(FIFO 实现盈亏 / 胜率 / 标签绩效 / 检查点分布)。

用法:
    python3 scripts/stats.py
    python3 scripts/stats.py --since 2026-08-01
    python3 scripts/stats.py --json

数据源:data/trades.tsv(唯一真相源,规范见 docs/DATA_CONTRACT.md)。
口径说明见 modes/stats.md —— 解读前必须先看口径。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass

from meigu_lib import LedgerError, Trade, load_vocabulary, parse_trades
from rules import audit_rules, load_rules

# 笔数低于这个数时,胜率与平均值是噪音,不做百分比解读。
MIN_SAMPLE = 20


@dataclass
class Match:
    """一个 FIFO 平仓配对。"""

    symbol: str
    qty: float
    buy_date: dt.date
    buy_price: float
    sell_date: dt.date
    sell_price: float
    sell_tag: str
    sell_checkpoint: str
    sell_line_no: int = 0

    @property
    def pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.qty

    @property
    def holding_days(self) -> int:
        return (self.sell_date - self.buy_date).days


def fifo_match(trades: list[Trade]) -> tuple[list[Match], list[str], dict[str, float]]:
    """FIFO 配对买卖,返回 (配对列表, 警告, 未平仓股数)。"""
    lots: dict[str, deque[list]] = defaultdict(deque)  # symbol -> deque([qty, price, date])
    matches: list[Match] = []
    warnings: list[str] = []

    for t in trades:
        if t.side == "buy":
            lots[t.symbol].append([t.qty, t.price, t.date])
            continue

        remaining = t.qty
        queue = lots[t.symbol]
        while remaining > 1e-9:
            if not queue:
                warnings.append(
                    f"第 {t.line_no} 行:{t.symbol} 卖出 {remaining:.6f} 股但台账里没有对应买入记录。"
                    f"台账缺一笔会让整段 FIFO 配对出错 —— 用 get_pnl_trade_history 回补,不要删行。"
                )
                break
            lot = queue[0]
            take = min(remaining, lot[0])
            matches.append(
                Match(
                    symbol=t.symbol,
                    qty=take,
                    buy_date=lot[2],
                    buy_price=lot[1],
                    sell_date=t.date,
                    sell_price=t.price,
                    sell_tag=t.reason_tag,
                    sell_checkpoint=t.checkpoint,
                    sell_line_no=t.line_no,
                )
            )
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                queue.popleft()

    open_qty = {sym: sum(l[0] for l in q) for sym, q in lots.items() if sum(l[0] for l in q) > 1e-9}
    return matches, warnings, open_qty


def summarize(trades: list[Trade], vocab=None) -> dict:
    vocab = vocab or load_vocabulary()
    matches, warnings, open_qty = fifo_match(trades)

    buys = [t for t in trades if t.side == "buy"]
    sells = [t for t in trades if t.side == "sell"]
    realized = sum(m.pnl for m in matches)
    wins = [m for m in matches if m.pnl > 0]

    # --- 按 reason_tag
    by_tag: dict[str, dict] = {}
    for tag in vocab.buy:
        rows = [t for t in trades if t.reason_tag == tag]
        if rows:
            by_tag[tag] = {
                "side": "buy",
                "count": len(rows),
                "events": len(rows),
                "amount": sum(t.amount for t in rows),
                "pnl": None,  # 买入标签不归集盈亏
                "win_rate": None,
            }
    for tag in vocab.sell:
        ms = [m for m in matches if m.sell_tag == tag]
        rows = [t for t in trades if t.reason_tag == tag]
        if not rows:
            continue
        # ★ 样本单位是**决策事件**(一笔卖出 = 一次退出决策),不是 FIFO lot 数量:
        # 一笔卖单可能匹配三个历史买入批次,那仍然只是一个决策。
        per_event: dict[int, float] = {}
        for m in ms:
            per_event[m.sell_line_no] = per_event.get(m.sell_line_no, 0.0) + m.pnl
        vals = list(per_event.values())
        by_tag[tag] = {
            "side": "sell",
            "count": len(rows),
            "events": len(vals),
            "lots": len(ms),
            "amount": sum(t.amount for t in rows),
            "pnl": sum(vals) if vals else 0.0,
            "win_rate": (len([v for v in vals if v > 0]) / len(vals) * 100) if vals else None,
            "avg_pnl": (sum(vals) / len(vals)) if vals else None,
        }

    # --- 按检查点
    by_checkpoint: dict[str, dict] = {}
    for t in trades:
        cp = t.checkpoint or "(未记录)"
        entry = by_checkpoint.setdefault(cp, {"count": 0, "buy": 0, "sell": 0, "pnl": 0.0})
        entry["count"] += 1
        entry[t.side] += 1
    for m in matches:
        cp = m.sell_checkpoint or "(未记录)"
        by_checkpoint.setdefault(cp, {"count": 0, "buy": 0, "sell": 0, "pnl": 0.0})
        by_checkpoint[cp]["pnl"] += m.pnl

    # --- 按月
    by_month: dict[str, dict] = {}
    for t in trades:
        key = t.date.strftime("%Y-%m")
        entry = by_month.setdefault(key, {"count": 0, "pnl": 0.0})
        entry["count"] += 1
    for m in matches:
        key = m.sell_date.strftime("%Y-%m")
        by_month.setdefault(key, {"count": 0, "pnl": 0.0})
        by_month[key]["pnl"] += m.pnl

    # --- 按标的
    by_symbol: dict[str, dict] = {}
    for t in trades:
        entry = by_symbol.setdefault(t.symbol, {"count": 0, "pnl": 0.0})
        entry["count"] += 1
    for m in matches:
        by_symbol.setdefault(m.symbol, {"count": 0, "pnl": 0.0})
        by_symbol[m.symbol]["pnl"] += m.pnl

    return {
        "trade_count": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_amount": sum(t.amount for t in buys),
        "sell_amount": sum(t.amount for t in sells),
        "first_date": trades[0].date.isoformat() if trades else None,
        "last_date": trades[-1].date.isoformat() if trades else None,
        "closed_pairs": len(matches),
        "realized_pnl": realized,
        "win_rate": (len(wins) / len(matches) * 100) if matches else None,
        "avg_holding_days": (
            sum(m.holding_days for m in matches) / len(matches) if matches else None
        ),
        "best": max(matches, key=lambda m: m.pnl) if matches else None,
        "worst": min(matches, key=lambda m: m.pnl) if matches else None,
        "sample_sufficient": len(matches) >= MIN_SAMPLE,
        "by_tag": by_tag,
        "by_checkpoint": by_checkpoint,
        "by_month": by_month,
        "by_symbol": by_symbol,
        "open_positions": open_qty,
        "warnings": warnings,
    }


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


RULES_PATH_OVERRIDE = None
VOCAB_OVERRIDE = None


def print_report(s: dict) -> None:
    print("=== meigu-ops 台账统计 ===")
    if not s["trade_count"]:
        print("\ndata/trades.tsv 里还没有交易记录。")
        print("字段规范见 docs/DATA_CONTRACT.md;示例见 examples/sample-trades.tsv。")
        return

    print(f"区间:{s['first_date']} ~ {s['last_date']}")
    print(
        f"交易 {s['trade_count']} 笔({s['buy_count']} 买 / {s['sell_count']} 卖)· "
        f"买入额 ${s['buy_amount']:,.2f} / 卖出额 ${s['sell_amount']:,.2f}"
    )

    print("\n--- 一、实现盈亏(FIFO,只含已平仓部分)---")
    print(f"平仓配对:{s['closed_pairs']} 个 · 实现盈亏:{_money(s['realized_pnl'])}")
    if s["closed_pairs"]:
        if s["sample_sufficient"]:
            print(
                f"胜率:{s['win_rate']:.1f}% · 平均持有:{s['avg_holding_days']:.1f} 自然日"
            )
        else:
            print(
                f"胜率:{s['win_rate']:.1f}% · 平均持有:{s['avg_holding_days']:.1f} 自然日"
            )
            print(
                f"⚠️  仅 {s['closed_pairs']} 个平仓配对(<{MIN_SAMPLE}),"
                f"胜率与平均值是噪音 —— 只看绝对盈亏和单笔最大亏损,不要解读百分比。"
            )
        b, w = s["best"], s["worst"]
        print(f"最佳:{b.symbol} {_money(b.pnl)}(持有 {b.holding_days} 天,{b.sell_tag})")
        print(f"最差:{w.symbol} {_money(w.pnl)}(持有 {w.holding_days} 天,{w.sell_tag})")

    if s["open_positions"]:
        print("\n未平仓(不计入上面的实现盈亏):")
        for sym, qty in sorted(s["open_positions"].items()):
            print(f"  {sym}: {qty:.6f} 股")

    print("\n--- 二、标签绩效(纪律体检)---")
    print(f"{'reason_tag':<12} {'笔数':>4} {'金额':>12} {'实现盈亏':>12} {'胜率':>7} {'均笔':>10}")
    for tag, d in s["by_tag"].items():
        wr = f"{d['win_rate']:.0f}%" if d.get("win_rate") is not None else "—"
        avg = _money(d.get("avg_pnl")) if d.get("avg_pnl") is not None else "—"
        print(
            f"{tag:<12} {d['count']:>4} {'$' + format(d['amount'], ',.2f'):>12} "
            f"{_money(d['pnl']):>12} {wr:>7} {avg:>10}"
        )
    print("  注:买入标签只统计笔数与金额,盈亏按卖出标签归集。")

    # 纪律审计 —— 检验**你自己写在 config/rules.toml 里的规则**。
    # 本仓库不预设任何市场判断类规则(见 config/rules.example.toml)。
    try:
        rule_list, src, is_example = load_rules(path=RULES_PATH_OVERRIDE, vocab=VOCAB_OVERRIDE)
    except Exception as exc:                      # noqa: BLE001
        print(f"\n  ⚠️  规则文件有问题,跳过纪律审计:{exc}")
    else:
        market = [r for r in rule_list if r.kind == "market" and r.active]
        print(f"\n  ▸ 纪律审计({src})")
        if not market:
            print("      尚未定义任何市场判断类规则。")
            print("      cp config/rules.example.toml config/rules.toml 并回答里面的问题 ——")
            print("      定义了可检验的规则,这里才能告诉你它们是否被数据支持。")
        for v in audit_rules(market, s):
            scope = "" if v.rule.may_authorize_live else "  [观察期,不可单独授权下单]"
            print(f"      {v.icon} [{v.label}] {v.rule.statement}{scope}")
            if v.detail:
                print(f"          {v.detail}")
            if v.suggestion:
                print(f"          → {v.suggestion}")

    print("\n--- 三、检查点分布(流程体检)---")
    print(f"{'检查点':<12} {'笔数':>4} {'买':>4} {'卖':>4} {'实现盈亏':>12}")
    total = s["trade_count"]
    for cp, d in sorted(s["by_checkpoint"].items()):
        print(f"{cp:<12} {d['count']:>4} {d['buy']:>4} {d['sell']:>4} {_money(d['pnl']):>12}")
    if total:
        top = max(s["by_checkpoint"].items(), key=lambda kv: kv[1]["count"])
        print(
            f"\n  操作最集中在 {top[0]}({top[1]['count'] / total * 100:.0f}%)。"
            f"\n  某个检查点的实现盈亏若系统性为负,是该时点的决策质量问题,不是运气。"
        )

    print("\n--- 四、月度 ---")
    for month, d in sorted(s["by_month"].items()):
        print(f"  {month}: {d['count']:>3} 笔 · {_money(d['pnl'])}")

    print("\n--- 五、按标的 ---")
    for sym, d in sorted(s["by_symbol"].items(), key=lambda kv: -kv[1]["pnl"]):
        print(f"  {sym:<6} {d['count']:>3} 笔 · {_money(d['pnl'])}")

    if s["warnings"]:
        print(f"\n⚠️  {len(s['warnings'])} 个台账警告:")
        for w in s["warnings"]:
            print(f"  · {w}")

    print("\n口径与解读框架见 modes/stats.md。盈亏表看不到的隐性成本"
          "(现金闲置的机会成本、市价单滑点)必须在解读时主动提出。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="交易台账绩效统计")
    ap.add_argument("--since", help="只统计该日期(含)之后的交易,YYYY-MM-DD")
    ap.add_argument("--until", help="只统计该日期(含)之前的交易,YYYY-MM-DD")
    ap.add_argument("--symbol", help="只统计该标的")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--file", help="指定台账路径(默认 data/trades.tsv)")
    ap.add_argument("--demo", action="store_true",
                    help="用 examples/ 的虚构固件(台账 + 词表 + 规则)")
    args = ap.parse_args(argv)

    from pathlib import Path as _P

    from meigu_lib import ROOT as _ROOT

    ex = _ROOT / "examples"
    vocab = load_vocabulary(path=ex / "sample-reason-tags.toml") if args.demo else None
    rules_path = (ex / "sample-rules.toml") if args.demo else None
    if args.demo and not args.file:
        args.file = str(ex / "sample-trades.tsv")

    try:
        path = _P(args.file) if args.file else None
        trades = parse_trades(path, vocab=vocab)
    except LedgerError as exc:
        print(f"❌ 台账格式错误:\n{exc}", file=sys.stderr)
        print(
            "\n不要为了让脚本跑过就删行 —— 台账缺一笔,整段历史的 FIFO 配对都会错。"
            "\n用 get_pnl_trade_history / get_equity_orders 回补。",
            file=sys.stderr,
        )
        return 2

    if not args.demo:
        # 演练进行中时为 review 环节留一条机器证据;不在演练中则无感。
        try:
            from setup import append_drill_evidence

            append_drill_evidence("review", f"台账 {len(trades)} 笔已解析并统计",
                                  ok=len(trades) > 0)
        except Exception:                         # noqa: BLE001
            pass

    if args.since:
        since = dt.date.fromisoformat(args.since)
        trades = [t for t in trades if t.date >= since]
    if args.until:
        until = dt.date.fromisoformat(args.until)
        trades = [t for t in trades if t.date <= until]
    if args.symbol:
        trades = [t for t in trades if t.symbol == args.symbol.upper()]

    s = summarize(trades, vocab=vocab)

    if args.json:
        out = dict(s)
        for k in ("best", "worst"):
            m = out[k]
            out[k] = (
                None
                if m is None
                else {
                    "symbol": m.symbol,
                    "pnl": m.pnl,
                    "holding_days": m.holding_days,
                    "sell_tag": m.sell_tag,
                }
            )
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    global RULES_PATH_OVERRIDE, VOCAB_OVERRIDE
    RULES_PATH_OVERRIDE = rules_path
    VOCAB_OVERRIDE = vocab
    print_report(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
