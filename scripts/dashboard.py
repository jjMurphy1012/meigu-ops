#!/usr/bin/env python3
"""meigu-ops 只读仪表盘(curses TUI,仅标准库)。

用法:
    python3 scripts/dashboard.py            # 读 data/(真实数据)
    python3 scripts/dashboard.py --demo     # 读 examples/(全虚构数据,录 gif 用)
    python3 scripts/dashboard.py --render 组合 --width 96   # 非交互:打印一屏,供测试/CI

按键:
    ⇥ / 1-3   切标签      ↑↓ / j k   移动光标
    s         切排序       Enter      看详情(台账页)
    Esc       关详情       r          重新载入
    q         退出         ?          帮助

三个标签页
==========
  组合  —— 持仓占比条 / BP vs 目标 / 集中度告警(每天要看的)
  台账  —— data/trades.tsv,可排序,Enter 看该笔的 FIFO 配对与实现盈亏
  纪律  —— 按理由标签的绩效对比 + 核心纪律是否被数据支持

★ 设计:全部渲染是**纯函数** `render_*(data, width, height, state) -> list[str]`,
curses 只负责把字符串贴到屏幕上。这样整个 UI 可以被单元测试覆盖 ——
本项目的教训是"防线必须有测试",UI 也算防线的一部分(它会被人当成事实来读)。

★ 只读:这个面板不下单、不改任何文件。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from meigu_lib import (
    DATA_DIR,
    ROOT,
    LedgerError,
    Trade,
    load_config,
    load_vocabulary,
    parse_trades,
)
from rules import audit_rules, load_rules
from stats import fifo_match, summarize

EXAMPLES_DIR = ROOT / "examples"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

TABS = ("组合", "台账", "纪律")

FULL, EMPTY = "▓", "░"

SORT_KEYS = (
    ("date", "日期"),
    ("symbol", "标的"),
    ("amount", "金额"),
    ("reason_tag", "标签"),
)


# ------------------------------------------------------------------ 宽度与格式
def dwidth(s: str) -> int:
    """显示宽度 —— 中文/全角算 2 列,否则表格永远对不齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, n: int, align: str = "<") -> str:
    """按显示宽度左/右/居中填充或截断,返回**精确** n 列。

    截断分支必须补齐:被跳过的若是双宽字符,`out + "…"` 会比 n 少 1 列 ——
    表格就此错位。tests/test_dashboard.py 的宽度不变量专门盯这个。
    """
    if n <= 0:
        return ""
    w = dwidth(s)
    if w > n:
        out = ""
        for ch in s:
            if dwidth(out) + dwidth(ch) > n - 1:
                break
            out += ch
        out += "…"
        return out + " " * (n - dwidth(out))
    space = n - w
    if align == ">":
        return " " * space + s
    if align == "^":
        left = space // 2
        return " " * left + s + " " * (space - left)
    return s + " " * space


def money(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    if signed:
        return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    return f"${v:,.2f}"


def pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{'+' if signed and v >= 0 else ''}{v:.1f}%"


def bar(value_pct: float, width: int, cap_pct: float | None = None) -> str:
    """占比条。cap_pct 给定时,超限用 ! 结尾提示。"""
    width = max(width, 1)
    filled = max(0, min(width, round(value_pct / 100 * width)))
    s = FULL * filled + EMPTY * (width - filled)
    if cap_pct is not None and value_pct > cap_pct:
        s = s[:-1] + "!" if width > 1 else "!"
    return s


# ---------------------------------------------------------------------- 数据层
@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float
    price: float
    market_value: float

    @property
    def pnl_pct(self) -> float | None:
        if not self.avg_cost:
            return None
        return (self.price - self.avg_cost) / self.avg_cost * 100

    @property
    def pnl_usd(self) -> float:
        return (self.price - self.avg_cost) * self.qty


@dataclass
class Snapshot:
    date: str = ""
    checkpoint: str = ""
    captured_at: str = ""
    total_value: float | None = None
    cash: float | None = None
    buying_power: float | None = None
    equity_value: float | None = None
    positions: list[Position] = field(default_factory=list)
    source: str = ""
    note: str = ""

    @property
    def bp_pct(self) -> float | None:
        if not self.total_value or self.buying_power is None:
            return None
        return self.buying_power / self.total_value * 100

    @property
    def total_pnl_usd(self) -> float:
        return sum(p.pnl_usd for p in self.positions)

    @property
    def cost_basis(self) -> float:
        return sum(p.avg_cost * p.qty for p in self.positions)

    @property
    def total_pnl_pct(self) -> float | None:
        cb = self.cost_basis
        return (self.total_pnl_usd / cb * 100) if cb else None

    def share_pct(self, p: Position) -> float | None:
        if not self.equity_value:
            return None
        return p.market_value / self.equity_value * 100


@dataclass
class DashboardData:
    snapshot: Snapshot
    trades: list[Trade]
    matches: list
    summary: dict
    cfg: dict
    demo: bool
    rules: list = field(default_factory=list)
    rules_source: str = ""
    rules_is_example: bool = True
    errors: list[str] = field(default_factory=list)


def _pick_latest_checkpoint(payload: dict) -> tuple[str, dict]:
    cps = payload.get("checkpoints") or {}
    if not cps:
        return "", {}
    key = sorted(cps)[-1]
    return key, cps[key] or {}


def parse_snapshot(payload: dict, source: str) -> Snapshot:
    """只认 normalized 块 —— 原始 MCP 返回的字段名不稳定,不猜。"""
    cp_name, cp = _pick_latest_checkpoint(payload)
    snap = Snapshot(
        date=str(payload.get("date", "")),
        checkpoint=cp_name,
        captured_at=str(cp.get("captured_at_et", "")),
        source=source,
    )
    norm = cp.get("normalized")
    if not isinstance(norm, dict):
        snap.note = (
            "快照缺少 normalized 块 —— dashboard 不猜原始 MCP 字段名。"
            "写快照时请一并提供归一化视图(见 docs/DATA_CONTRACT.md §4)。"
        )
        return snap

    snap.total_value = norm.get("total_value")
    snap.cash = norm.get("cash")
    snap.buying_power = norm.get("buying_power")
    snap.equity_value = norm.get("equity_value")
    for row in norm.get("positions") or []:
        try:
            snap.positions.append(
                Position(
                    symbol=str(row["symbol"]).upper(),
                    qty=float(row.get("qty", 0) or 0),
                    avg_cost=float(row.get("avg_cost", 0) or 0),
                    price=float(row.get("price", 0) or 0),
                    market_value=float(row.get("market_value", 0) or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            snap.note = "normalized.positions 里有无法解析的条目,已跳过。"
    snap.positions.sort(key=lambda p: -p.market_value)
    return snap


def load_dashboard_data(demo: bool = False) -> DashboardData:
    errors: list[str] = []

    if demo:
        snap_path = EXAMPLES_DIR / "sample-snapshot.json"
        trades_path = EXAMPLES_DIR / "sample-trades.tsv"
        vocab = load_vocabulary(path=EXAMPLES_DIR / "sample-reason-tags.toml")
        rules_path = EXAMPLES_DIR / "sample-rules.toml"
    else:
        vocab = load_vocabulary()
        rules_path = None
        snaps = sorted(SNAPSHOT_DIR.glob("*.json")) if SNAPSHOT_DIR.exists() else []
        snap_path = snaps[-1] if snaps else None
        trades_path = None  # parse_trades() 默认读 data/trades.tsv

    if snap_path and snap_path.exists():
        try:
            snapshot = parse_snapshot(
                json.loads(snap_path.read_text(encoding="utf-8")),
                str(snap_path.relative_to(ROOT)),
            )
        except json.JSONDecodeError as exc:
            snapshot = Snapshot(note=f"快照 JSON 解析失败:{exc}")
            errors.append(f"{snap_path.name}: {exc}")
    else:
        snapshot = Snapshot(
            note="还没有任何快照。检查点里用 scripts/snapshot.py 归档持仓与行情后,"
            "这一页就会有内容。"
        )

    try:
        trades = parse_trades(trades_path or None, vocab=vocab)
    except LedgerError as exc:
        trades = []
        errors.append(f"台账格式错误:{exc}")

    matches, warns, _open = fifo_match(trades)
    errors.extend(warns)

    try:
        rule_list, rules_src, rules_is_example = load_rules(path=rules_path, vocab=vocab)
    except Exception as exc:                      # noqa: BLE001
        rule_list, rules_src, rules_is_example = [], "(规则文件有问题)", True
        errors.append(f"config/rules.toml: {exc}")

    return DashboardData(
        snapshot=snapshot,
        trades=trades,
        matches=matches,
        summary=summarize(trades, vocab=vocab),
        cfg=load_config("profile", required=False),
        demo=demo,
        rules=rule_list,
        rules_source=rules_src,
        rules_is_example=rules_is_example,
        errors=errors,
    )


# ------------------------------------------------------------------ UI 状态
@dataclass
class UiState:
    tab: int = 0
    cursor: int = 0
    sort: int = 0
    sort_desc: bool = True
    detail: bool = False
    help: bool = False

    def row_count(self, data: DashboardData) -> int:
        if self.tab == 0:
            return len(data.snapshot.positions)
        if self.tab == 1:
            return len(data.trades)
        return len(discipline_rows(data.summary))

    def clamp(self, data: DashboardData) -> None:
        n = self.row_count(data)
        self.cursor = 0 if n == 0 else max(0, min(self.cursor, n - 1))


# ------------------------------------------------------------------ 渲染:通用
def render_header(data: DashboardData, width: int, state: UiState) -> list[str]:
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "?"
    tabs = "  ".join(
        (f"[{i + 1} {name}]" if i == state.tab else f" {i + 1} {name} ")
        for i, name in enumerate(TABS)
    )
    right = f"meigu-ops v{ver}" + ("  · DEMO 数据" if data.demo else "")
    line = pad(tabs, width - dwidth(right) - 1) + right
    return [pad(line, width), "─" * width]


def render_footer(data: DashboardData, width: int, state: UiState) -> list[str]:
    if state.help:
        keys = "任意键关闭帮助"
    elif state.detail:
        keys = "Esc 关详情   ↑↓ 移动   q 退出"
    elif state.tab == 1:
        keys = f"⇥ 标签   ↑↓ 移动   s 排序({SORT_KEYS[state.sort][1]}{'↓' if state.sort_desc else '↑'})   Enter 详情   r 重载   ? 帮助   q 退出"
    else:
        keys = "⇥ 标签   ↑↓ 移动   r 重载   ? 帮助   q 退出"
    warn = f"  ⚠ {len(data.errors)} 条数据警告" if data.errors else ""
    return ["─" * width, pad(keys + warn, width)]


def render_help(width: int, height: int) -> list[str]:
    body = [
        "meigu-ops dashboard —— 只读面板(不下单、不改文件)",
        "",
        "  ⇥ / 1-3      切换标签页",
        "  ↑↓ / j k     移动光标",
        "  s            台账页:切换排序列",
        "  S            反转排序方向",
        "  Enter        台账页:查看该笔的 FIFO 配对与实现盈亏",
        "  Esc          关闭详情",
        "  r            重新载入数据",
        "  ?            这个帮助",
        "  q            退出",
        "",
        "标签页:",
        "  组合  持仓占比 / BP vs 目标 / 集中度(读最新快照的 normalized 块)",
        "  台账  data/trades.tsv 的每一笔,可排序",
        "  纪律  按 reason_tag 的绩效对比 —— 核心纪律是否被数据支持",
        "",
        "口径与解读框架见 modes/stats.md。数据源见 docs/DATA_CONTRACT.md。",
        "本面板不构成投资建议。",
    ]
    return [pad(b, width) for b in body][: height]


# ------------------------------------------------------- 渲染:组合
def render_portfolio(data: DashboardData, width: int, height: int, state: UiState) -> list[str]:
    s = data.snapshot
    cash_cfg = data.cfg.get("cash", {})
    pos_cfg = data.cfg.get("position", {})
    bp_target = float(cash_cfg.get("bp_target_pct", 20))
    max_single = float(pos_cfg.get("max_single_pct", 50))

    out: list[str] = []

    if not s.positions:
        out.append("")
        for line in (s.note or "无持仓数据。").split("。"):
            if line.strip():
                out.append(pad("  " + line.strip() + "。", width))
        return out[:height]

    stamp = f"{s.date} {s.checkpoint}" if s.date else "—"
    out.append(pad(f"  快照 {stamp}   来源 {s.source}", width))

    bp = s.bp_pct
    bp_state = "—"
    if bp is not None:
        ok = bp < bp_target
        bp_state = f"{bar(bp, 10, bp_target)} {bp:.1f}% 目标<{bp_target:.0f}% {'✅' if ok else '⚠ 高于你设定的目标'}"
    out.append(
        pad(
            f"  总值 {money(s.total_value)}   现金 {money(s.cash)}   "
            f"浮盈亏 {money(s.total_pnl_usd, signed=True)}({pct(s.total_pnl_pct)})",
            width,
        )
    )
    out.append(pad(f"  BP   {bp_state}", width))
    out.append("")

    cols = [("标的", 7, "<"), ("股数", 9, ">"), ("成本", 10, ">"), ("现价", 10, ">"),
            ("浮盈亏", 9, ">"), ("市值", 10, ">"), ("占股票市值", 24, "<")]
    out.append("  " + " ".join(pad(c[0], c[1], "^") for c in cols))
    out.append("  " + " ".join("─" * c[1] for c in cols))

    for i, p in enumerate(s.positions):
        share = s.share_pct(p)
        share_cell = "—"
        if share is not None:
            flag = " ⚠超上限" if share > max_single else ""
            share_cell = f"{bar(share, 9, max_single)} {share:4.1f}%{flag}"
        cursor = "▸" if i == state.cursor else " "
        out.append(
            cursor + " " + " ".join([
                pad(p.symbol, cols[0][1]),
                pad(f"{p.qty:.4f}", cols[1][1], ">"),
                pad(money(p.avg_cost), cols[2][1], ">"),
                pad(money(p.price), cols[3][1], ">"),
                pad(pct(p.pnl_pct), cols[4][1], ">"),
                pad(money(p.market_value), cols[5][1], ">"),
                pad(share_cell, cols[6][1]),
            ])
        )

    out.append("")
    top = max((s.share_pct(p) or 0) for p in s.positions)
    verdict = "✅ 集中度在上限内" if top <= max_single else f"⚠ 最大单一标的 {top:.1f}% 超过上限 {max_single:.0f}%"
    out.append(pad(f"  {verdict}   持仓 {len(s.positions)} 个", width))
    if s.note:
        out.append(pad(f"  注:{s.note}", width))
    return out[:height]


# ------------------------------------------------------- 渲染:台账
def sort_trades(trades: list[Trade], sort_idx: int, desc: bool) -> list[Trade]:
    key = SORT_KEYS[sort_idx][0]
    if key == "date":
        fn = lambda t: (t.date, t.line_no)          # noqa: E731
    elif key == "symbol":
        fn = lambda t: (t.symbol, t.date)           # noqa: E731
    elif key == "amount":
        fn = lambda t: (t.amount, t.date)           # noqa: E731
    else:
        fn = lambda t: (t.reason_tag, t.date)       # noqa: E731
    return sorted(trades, key=fn, reverse=desc)


def trade_pnl(trade: Trade, matches: list) -> float | None:
    """该卖出行归集到的 FIFO 实现盈亏。买入行返回 None。"""
    if trade.side != "sell":
        return None
    hit = [m for m in matches
           if m.sell_date == trade.date and m.symbol == trade.symbol
           and m.sell_tag == trade.reason_tag]
    return sum(m.pnl for m in hit) if hit else None


def render_ledger(data: DashboardData, width: int, height: int, state: UiState) -> list[str]:
    out: list[str] = []
    if not data.trades:
        out.append("")
        out.append(pad("  data/trades.tsv 里还没有交易记录。", width))
        out.append(pad("  字段规范见 docs/DATA_CONTRACT.md §2;示例见 examples/sample-trades.tsv。", width))
        return out[:height]

    rows = sort_trades(data.trades, state.sort, state.sort_desc)
    s = data.summary
    out.append(
        pad(
            f"  {s['trade_count']} 笔({s['buy_count']} 买 / {s['sell_count']} 卖)   "
            f"实现盈亏 {money(s['realized_pnl'], signed=True)}   "
            f"平仓配对 {s['closed_pairs']} 个"
            + ("" if s["sample_sufficient"] else "   ⚠ 样本不足,勿解读百分比"),
            width,
        )
    )
    out.append("")

    cols = [("日期", 10, "<"), ("点", 5, "<"), ("标的", 6, "<"), ("向", 4, "<"),
            ("股数", 9, ">"), ("成交价", 9, ">"), ("金额", 9, ">"),
            ("理由标签", 12, "<"), ("占仓位", 7, ">"), ("实现盈亏", 10, ">")]
    out.append("  " + " ".join(pad(c[0], c[1], "^") for c in cols))
    out.append("  " + " ".join("─" * c[1] for c in cols))

    body_h = max(1, height - len(out) - 1)
    start = max(0, min(state.cursor - body_h // 2, len(rows) - body_h))
    start = max(0, start)

    for i in range(start, min(start + body_h, len(rows))):
        t = rows[i]
        p = trade_pnl(t, data.matches)
        cursor = "▸" if i == state.cursor else " "
        out.append(
            cursor + " " + " ".join([
                pad(t.date.isoformat(), cols[0][1]),
                pad(t.checkpoint, cols[1][1]),
                pad(t.symbol, cols[2][1]),
                pad("买" if t.side == "buy" else "卖", cols[3][1]),
                pad(f"{t.qty:.4f}", cols[4][1], ">"),
                pad(money(t.price), cols[5][1], ">"),
                pad(money(t.amount), cols[6][1], ">"),
                pad(t.reason_tag, cols[7][1]),
                pad(f"{t.pct_of_position:.0f}%" if t.pct_of_position is not None else "—",
                    cols[8][1], ">"),
                pad(money(p, signed=True) if p is not None else "—", cols[9][1], ">"),
            ])
        )
    if len(rows) > body_h:
        out.append(pad(f"  第 {state.cursor + 1}/{len(rows)} 笔", width))
    return out[:height]


def render_trade_detail(data: DashboardData, width: int, height: int, state: UiState) -> list[str]:
    rows = sort_trades(data.trades, state.sort, state.sort_desc)
    if not rows:
        return [pad("  无数据", width)]
    t = rows[min(state.cursor, len(rows) - 1)]
    side = "买入" if t.side == "buy" else "卖出"

    out = ["", pad(f"  ── {t.date} {t.checkpoint}  {side} {t.symbol} ──", width), ""]
    for label, value in (
        ("股数", f"{t.qty:.6f}"),
        ("成交价", money(t.price)),
        ("金额", money(t.amount)),
        ("理由标签", t.reason_tag),
        ("占该仓位市值", f"{t.pct_of_position:.1f}%" if t.pct_of_position is not None else "未记录"),
        ("台账行号", str(t.line_no)),
    ):
        out.append(pad(f"    {pad(label, 14)}{value}", width))

    if t.side == "sell":
        hit = [m for m in data.matches
               if m.sell_date == t.date and m.symbol == t.symbol
               and m.sell_tag == t.reason_tag]
        out.append("")
        out.append(pad("  FIFO 配对(先买的先出):", width))
        if not hit:
            out.append(pad("    无配对 —— 台账里可能缺少对应买入记录。", width))
        for m in hit:
            out.append(
                pad(
                    f"    买入 {m.buy_date} @ {money(m.buy_price)}  ×{m.qty:.6f}  "
                    f"→ 持有 {m.holding_days} 天  实现 {money(m.pnl, signed=True)}",
                    width,
                )
            )
        if hit:
            out.append("")
            out.append(pad(f"    合计实现盈亏 {money(sum(m.pnl for m in hit), signed=True)}", width))
    else:
        out.append("")
        out.append(pad("  买入行不归集盈亏 —— 盈亏按卖出行的 reason_tag 归集(见 modes/stats.md)。", width))

    if t.note:
        out.append("")
        out.append(pad(f"  备注:{t.note}", width))
    return out[:height]


# ------------------------------------------------------- 渲染:纪律
def discipline_rows(summary: dict) -> list[tuple]:
    rows = []
    for tag, d in (summary.get("by_tag") or {}).items():
        rows.append((
            tag,
            d.get("side", ""),
            d.get("count", 0),
            d.get("amount", 0.0),
            d.get("pnl"),
            d.get("win_rate"),
            d.get("avg_pnl"),
        ))
    return rows


def core_rule_verdicts(summary: dict, rule_list: list | None = None) -> list[tuple[str, str, str]]:
    """(规则陈述, 检验数据, 判定) —— 全部来自用户的 config/rules.toml。

    ⚠️ 本仓库**不预设任何市场判断类规则**。这里显示什么,取决于用户自己
    在 rules.toml 里声明了哪些可检验的命题(见 config/rules.example.toml)。
    """
    if rule_list is None:
        try:
            rule_list, _, _ = load_rules()
        except Exception:                         # noqa: BLE001
            return []
    market = [r for r in rule_list if r.kind == "market" and r.active]
    return [
        (v.rule.statement, v.detail, v.label, v.icon, v.rule.may_authorize_live)
        for v in audit_rules(market, summary)
    ]


def render_discipline(data: DashboardData, width: int, height: int, state: UiState) -> list[str]:
    s = data.summary
    rows = discipline_rows(s)
    out: list[str] = []

    if not rows:
        out.append("")
        out.append(pad("  台账为空 —— 纪律体检需要有交易记录才能做。", width))
        return out[:height]

    out.append(pad("  按理由标签的绩效(盈亏按卖出标签归集)", width))
    out.append("")
    cols = [("理由标签", 12, "<"), ("向", 4, "<"), ("笔数", 6, ">"),
            ("金额", 10, ">"), ("实现盈亏", 10, ">"), ("胜率", 6, ">"), ("均笔", 10, ">")]
    out.append("  " + " ".join(pad(c[0], c[1], "^") for c in cols))
    out.append("  " + " ".join("─" * c[1] for c in cols))
    for i, (tag, side, cnt, amt, pnl, wr, av) in enumerate(rows):
        cursor = "▸" if i == state.cursor else " "
        out.append(
            cursor + " " + " ".join([
                pad(tag, cols[0][1]),
                pad("买" if side == "buy" else "卖", cols[1][1]),
                pad(str(cnt), cols[2][1], ">"),
                pad(money(amt), cols[3][1], ">"),
                pad(money(pnl, signed=True) if pnl is not None else "—", cols[4][1], ">"),
                pad(f"{wr:.0f}%" if wr is not None else "—", cols[5][1], ">"),
                pad(money(av, signed=True) if av is not None else "—", cols[6][1], ">"),
            ])
        )

    out.append("")
    out.append(pad(f"  ── 你的规则是否被数据支持({data.rules_source})──", width))

    verdicts = core_rule_verdicts(s, data.rules)
    if not verdicts:
        out.append(pad("  尚未定义市场判断类规则 —— 本仓库刻意不预设任何策略。", width))
        out.append(pad("  cp config/rules.example.toml config/rules.toml 并回答里面的问题,", width))
        out.append(pad("  这里就会告诉你每条规则是否被你自己的台账数据支持。", width))
    else:
        for rule, detail, label, icon, live in verdicts:
            tail = "" if live else "  [观察期]"
            out.append(pad(f"  {icon} [{label}] {rule}{tail}", width))
            if detail:
                out.append(pad(f"       {detail}", width))

    out.append("")
    out.append(pad("  一条从未被数据支持过的规则是包袱,不是资产 —— 见 modes/review.md。", width))
    return out[:height]


# ------------------------------------------------------- 组装一屏
def render_screen(data: DashboardData, width: int, height: int, state: UiState) -> list[str]:
    header = render_header(data, width, state)
    footer = render_footer(data, width, state)
    body_h = max(1, height - len(header) - len(footer))

    if state.help:
        body = render_help(width, body_h)
    elif state.detail and state.tab == 1:
        body = render_trade_detail(data, width, body_h, state)
    elif state.tab == 0:
        body = render_portfolio(data, width, body_h, state)
    elif state.tab == 1:
        body = render_ledger(data, width, body_h, state)
    else:
        body = render_discipline(data, width, body_h, state)

    body = [pad(b, width) for b in body[:body_h]]
    body += [" " * width] * (body_h - len(body))
    return header + body + footer


# ------------------------------------------------------------------- curses 层
def _curses_loop(stdscr, data: DashboardData, demo: bool) -> None:  # pragma: no cover
    import curses

    curses.curs_set(0)
    stdscr.keypad(True)
    state = UiState()

    while True:
        height, width = stdscr.getmaxyx()
        lines = render_screen(data, max(width - 1, 40), max(height, 12), state)
        stdscr.erase()
        # 画满 height 行(含最后一行的快捷键栏),但永不写最后一行的最右一格 ——
        # 写它会触发滚动。此前用 lines[:height-1] 直接把快捷键栏整行丢掉了。
        for y, line in enumerate(lines[:height]):
            try:
                attr = curses.A_REVERSE if y in (0, 1) else curses.A_NORMAL
                if line.lstrip().startswith("▸"):
                    attr = curses.A_BOLD
                stdscr.addstr(y, 0, line[: width - 1], attr)
            except curses.error:
                pass
        stdscr.refresh()

        ch = stdscr.getch()
        if state.help:
            state.help = False
            continue
        if ch in (ord("q"), ord("Q")):
            return
        if ch == ord("?"):
            state.help = True
        elif ch in (9, curses.KEY_RIGHT):        # Tab
            state.tab = (state.tab + 1) % len(TABS)
            state.detail = False
            state.clamp(data)
        elif ch == curses.KEY_LEFT:
            state.tab = (state.tab - 1) % len(TABS)
            state.detail = False
            state.clamp(data)
        elif ch in (ord("1"), ord("2"), ord("3")):
            state.tab = ch - ord("1")
            state.detail = False
            state.clamp(data)
        elif ch in (curses.KEY_DOWN, ord("j")):
            state.cursor += 1
            state.clamp(data)
        elif ch in (curses.KEY_UP, ord("k")):
            state.cursor -= 1
            state.clamp(data)
        elif ch == ord("s"):
            state.sort = (state.sort + 1) % len(SORT_KEYS)
        elif ch == ord("S"):
            state.sort_desc = not state.sort_desc
        elif ch in (10, 13, curses.KEY_ENTER):
            if state.tab == 1:
                state.detail = True
        elif ch == 27:                            # Esc
            state.detail = False
        elif ch == ord("r"):
            data = load_dashboard_data(demo)
            state.clamp(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="meigu-ops 只读仪表盘")
    ap.add_argument("--demo", action="store_true",
                    help="用 examples/ 的虚构数据(录 gif / 演示用)")
    ap.add_argument("--render", metavar="TAB",
                    help="非交互:渲染一屏并打印(组合 / 台账 / 纪律)")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--height", type=int, default=32)
    args = ap.parse_args(argv)

    data = load_dashboard_data(args.demo)

    if args.render:
        if args.render not in TABS:
            print(f"--render 只能是 {' / '.join(TABS)}", file=sys.stderr)
            return 2
        state = UiState(tab=TABS.index(args.render))
        print("\n".join(render_screen(data, args.width, args.height, state)))
        return 0

    if not sys.stdout.isatty():
        print("这是交互式 TUI,需要在终端里运行。", file=sys.stderr)
        print("非交互场景请用 --render 组合 / --render 台账 / --render 纪律。", file=sys.stderr)
        return 2

    import curses

    curses.wrapper(_curses_loop, data, args.demo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
