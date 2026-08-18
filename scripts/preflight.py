#!/usr/bin/env python3
"""下单前置检查 —— 确定性闸门,返回 ALLOW / DRY_RUN / DENY。

用法:
    python3 scripts/preflight.py --order-file order.json
    echo '<json>' | python3 scripts/preflight.py --stdin
    python3 scripts/preflight.py --stdin --json      # 机器可读

退出码:0 = ALLOW 或 DRY_RUN(可继续) · 1 = DENY(禁止下单) · 2 = 输入错误

为什么要这个脚本
================
v5.0.0 把"机械活下沉到脚本"写进了架构原则,却把**最有后果的**那几个机械检查
(尺寸占比、市场时段、意图时效、单日上限)留成了 `modes/trade.md` 里的散文,
全靠 agent 自觉遵守。而这些检查每一条都对应过真实损失:

  · 2026-07-29 套用标准尺寸到已缩水的仓位 → 实际卖出 91%,近乎清仓
  · 2026-07-13 分析与下单之间休眠 8.5h → 靠人眼核对报价时间戳才刹住
  · 2026-07-09 权限弹窗冻结会话 6h → 解冻后若执行陈旧意图会更糟

散文约束在 agent 状态好的时候有效。程序约束一直有效。

它不做什么
==========
不查行情、不判断该不该买、不调用券商 API。它只回答一个问题:
**"如果现在下这笔单,有没有违反任何一条可机械判定的硬约束?"**
判断标的好坏仍是 `modes/check.md` 的事。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from meigu_lib import (
    ROOT,
    TRADES_TSV,
    LedgerError,
    day_info,
    load_config,
    load_vocabulary,
    now_et,
    parse_trades,
    today_et,
)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

ALLOW, DRY_RUN, DENY = "ALLOW", "DRY_RUN", "DENY"

# 闸门注册表 —— config/rules.toml 的 `enforced_by` 只能引用这里的名字。
# 写错闸门名会让规则显示「程序强制」但其实无人把守,所以 rules.py 会交叉校验。
GATE_NAMES = (
    "紧急停止开关",
    "下单授权",
    "账户身份",
    "市场时段",
    "意图时效",
    "报价时间戳熔断",
    "理由标签",
    "依据强度与尺寸",
    "单笔金额上限",
    "减仓占比",
    "残值仓",
    "买力充足",
    "集中度",
    "现金底线",
    "单日累计",
    "单日笔数",
    "同标的当日重复",
    "ref_id",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True          # False = 只警告,不阻断
    hint: str = ""


@dataclass
class Result:
    checks: list[Check] = field(default_factory=list)
    verdict: str = ALLOW

    def add(self, name: str, ok: bool, detail: str, *, fatal: bool = True, hint: str = "") -> None:
        self.checks.append(Check(name, ok, detail, fatal, hint))

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.fatal]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.fatal]


# --------------------------------------------------------------------- 输入解析
REQUIRED_FIELDS = ("symbol", "side", "reason_tag")


def _parse_et(value: str | None, field_name: str) -> dt.datetime | None:
    """解析 ET 时间戳。接受 ISO 或 'YYYY-MM-DD HH:MM[:SS]'。"""
    if not value:
        return None
    from zoneinfo import ZoneInfo

    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = dt.datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
            return naive.replace(tzinfo=ZoneInfo("America/New_York"))
        except ValueError:
            continue
    raise ValueError(f"{field_name} 无法解析为 ET 时间:{value!r}")


def validate_order(order: dict) -> list[str]:
    errs = [f"缺少必填字段 {f}" for f in REQUIRED_FIELDS if not order.get(f)]
    side = str(order.get("side", "")).lower()
    if side not in ("buy", "sell"):
        errs.append(f"side 必须是 buy 或 sell,实际 {order.get('side')!r}")
    if order.get("amount_usd") is None and order.get("qty") is None:
        errs.append("必须提供 amount_usd 或 qty 之一")
    return errs


# ----------------------------------------------------------------------- 各闸门
def check_kill_switch(r: Result, cfg: dict) -> None:
    path = ROOT / cfg.get("execution", {}).get("kill_switch_file", "data/HALTED")
    exists = path.exists()
    r.add(
        "紧急停止开关",
        not exists,
        f"{path.relative_to(ROOT)} 存在 —— 一切下单已禁止" if exists else "未触发",
        hint=f"确认要恢复交易再删除 {path.relative_to(ROOT)}。" if exists else "",
    )


def check_execution_enabled(r: Result, cfg: dict) -> None:
    ex = cfg.get("execution", {})
    enabled = bool(ex.get("enabled", False))
    r.add(
        "下单授权",
        enabled,
        "已开启" if enabled else "未开启 —— 仓库默认关闭",
        hint="授权是本地事实:要下真单请在 config/profile.toml 设 execution.enabled = true。"
        "打开前先读 DISCLAIMER.md。"
        if not enabled
        else "",
    )


def check_account(r: Result, cfg: dict, order: dict) -> None:
    """账户身份校验 —— 只用配置指定的子账户下单。"""
    configured = str(cfg.get("account", {}).get("id", "")).strip()
    supplied = str(order.get("account_id", "")).strip()
    if not supplied:
        r.add(
            "账户身份",
            False,
            "订单未带 account_id —— 无法核实是否下在正确的子账户",
            fatal=False,
            hint="建议在订单里带上 account_id,让 preflight 能挡住下错账户。",
        )
        return
    match = bool(configured) and supplied == configured
    r.add(
        "账户身份",
        match,
        f"订单账户 ***{supplied[-4:]} vs 配置 ***{configured[-4:] or '????'}",
        hint="订单账户与 config/profile.toml 的 account.id 不一致 —— 拒绝执行。"
        if not match
        else "",
    )


def check_market_session(r: Result, cfg: dict, order: dict, now: dt.datetime) -> None:
    """市场时段 + 订单类型的机制可行性。"""
    info = day_info(now.date())
    if not info.is_trading_day:
        r.add("市场时段", False, f"{now.date()} 非交易日 —— {info.reason}")
        return

    close = dt.time(13, 0) if info.is_half_day else dt.time(16, 0)
    t = now.time()
    in_regular = dt.time(9, 30) <= t < close

    order_type = str(order.get("order_type", "market")).lower()
    session = str(order.get("market_hours", "regular_hours")).lower()

    if in_regular:
        r.add(
            "市场时段",
            True,
            f"正常时段内({t.strftime('%H:%M')} ET,收盘 {close.strftime('%H:%M')})",
        )
        return

    # 盘前 7:00-9:30 / 盘后 close-19:30 属延长时段
    pre = dt.time(7, 0) <= t < dt.time(9, 30)
    post = close <= t < dt.time(19, 30)

    if not (pre or post):
        r.add("市场时段", False, f"{t.strftime('%H:%M')} ET 在任何可交易时段之外")
        return

    label = "盘前" if pre else "盘后"
    # 延长时段只支持限价单;且分数股需逐标的合格性(见 _mechanics.md §1)
    if order_type != "limit":
        r.add(
            "市场时段",
            False,
            f"{label}({t.strftime('%H:%M')} ET)只接受限价单,当前 order_type={order_type}",
            hint="延长时段各交易场所只支持限价单。要么改限价单,要么等正常时段。",
        )
        return
    if session == "regular_hours":
        r.add(
            "市场时段",
            False,
            f"{label}下单但 market_hours=regular_hours —— 该单不会在本时段成交",
            hint="延长时段需显式指定对应的 market_hours。",
        )
        return

    r.add("市场时段", True, f"{label}限价单({t.strftime('%H:%M')} ET)")
    r.add(
        "延长时段分数股合格性",
        False,
        "本脚本无法核实 —— 需要券商能力探测",
        fatal=False,
        hint="分数股在延长时段的可交易性按标的流动性逐一裁定,且 24 小时市场仅限整股。"
        "下单前用 get_equity_tradability 核实该标的,不要假设(见 _mechanics.md §1)。",
    )


def check_intent_freshness(r: Result, cfg: dict, order: dict, now: dt.datetime) -> None:
    ttl = int(cfg.get("execution", {}).get("intent_ttl_minutes", 15))
    ts = _parse_et(order.get("analysis_at_et"), "analysis_at_et")
    if ts is None:
        r.add(
            "意图时效",
            False,
            "订单未提供 analysis_at_et",
            hint="必须记录分析完成时刻,否则无法判断意图是否已过期。",
        )
        return
    age = (now - ts).total_seconds() / 60
    ok = 0 <= age <= ttl
    r.add(
        "意图时效",
        ok,
        f"分析距今 {age:.1f} 分钟(上限 {ttl})",
        hint="意图已过期 —— 回 modes/check.md 重新拉行情分析,严禁执行陈旧意图。"
        if age > ttl
        else ("analysis_at_et 在未来 —— 检查时钟。" if age < 0 else ""),
    )


def check_quote_timestamp(r: Result, cfg: dict, order: dict, now: dt.datetime) -> None:
    """时间戳熔断 —— 券商服务器时间是唯一可信时钟。"""
    limit = int(cfg.get("execution", {}).get("quote_max_age_minutes", 10))
    ts = _parse_et(order.get("quote_timestamp_et"), "quote_timestamp_et")
    if ts is None:
        r.add(
            "报价时间戳熔断",
            False,
            "订单未提供 quote_timestamp_et",
            hint="从 review 返回的 market_data_disclosure 读券商报价时间戳填进来。"
            "本机时钟可能因休眠停在旧时刻,这是唯一能发现的办法(2026-07-13 靠它刹住一单)。",
        )
        return
    age = (now - ts).total_seconds() / 60
    ok = -1 <= age <= limit
    r.add(
        "报价时间戳熔断",
        ok,
        f"券商报价距今 {age:.1f} 分钟(上限 {limit})",
        hint="报价过旧 —— 机器可能在分析与下单之间休眠过。立即中止并重新分析。"
        if age > limit
        else "",
    )


def _order_amount(order: dict) -> float | None:
    amt = order.get("amount_usd")
    if amt is not None:
        return float(amt)
    qty, price = order.get("qty"), order.get("price")
    if qty is not None and price is not None:
        return float(qty) * float(price)
    return None


def check_size(r: Result, cfg: dict, order: dict) -> None:
    ex, pos_cfg = cfg.get("execution", {}), cfg.get("position", {})
    amount = _order_amount(order)
    if amount is None:
        r.add("单笔金额", False, "无法确定金额(需要 amount_usd,或 qty + price)")
        return

    cap = float(ex.get("max_order_usd", 80))
    r.add("单笔金额上限", amount <= cap, f"${amount:.2f} / 上限 ${cap:.2f}")

    if amount <= 0:
        r.add("金额为正", False, f"金额必须 > 0,实际 {amount}")

    # --- 减仓占比:2026-07-29 就是在这一步失守(实际卖出 91%)
    position = order.get("position") or {}
    mv = position.get("market_value")
    if str(order.get("side")).lower() == "sell" and mv:
        mv = float(mv)
        pct = amount / mv * 100 if mv > 0 else 100.0
        warn_at = float(pos_cfg.get("reduce_pct_warn", 50))
        intent = str(order.get("intent", "")).lower()  # "partial" | "close" | ""
        closing = intent == "close" or pct >= 99.5
        r.add(
            "减仓占比",
            closing or pct <= warn_at,
            f"卖出 ${amount:.2f} 占该仓位市值 ${mv:.2f} 的 {pct:.1f}%(阈值 {warn_at:.0f}%)",
            hint=f"意图是部分减仓,但实际会卖掉 {pct:.1f}% —— 停下重算尺寸。"
            f"若确实要清仓,请在订单里写 intent=\"close\"。"
            if not closing and pct > warn_at
            else "",
        )

        # --- 残值仓:减完剩下的零头要么别减这么多,要么一次清干净
        std = float(cfg.get("trade", {}).get("size_std", 50))
        ratio = float(pos_cfg.get("residual_threshold_ratio", 0.5))
        remaining = mv - amount
        if not closing and 0 < remaining < std * ratio:
            r.add(
                "残值仓",
                False,
                f"减完剩余 ${remaining:.2f},低于残值线 ${std * ratio:.2f}",
                fatal=False,
                hint="要么减少卖出额,要么一次清干净 —— 残值仓不贡献分散/收益,还占一个持仓名额。",
            )


def check_concentration_and_bp(r: Result, cfg: dict, order: dict) -> None:
    if str(order.get("side")).lower() != "buy":
        return
    amount = _order_amount(order)
    if amount is None:
        return

    pf = order.get("portfolio") or {}
    position = order.get("position") or {}
    bp = pf.get("buying_power")
    equity = pf.get("equity_value")
    total = pf.get("total_value")

    if bp is not None:
        bp = float(bp)
        r.add(
            "买力充足",
            amount <= bp,
            f"需 ${amount:.2f} / 可用买力 ${bp:.2f}",
            hint="现金账户卖出资金 T+1 结算:看 buying_power,不是 cash。" if amount > bp else "",
        )

    if equity is not None and position.get("market_value") is not None:
        equity, mv = float(equity), float(position["market_value"])
        post_pct = (mv + amount) / (equity + amount) * 100 if equity + amount > 0 else 0
        cap = float(cfg.get("position", {}).get("max_single_pct", 50))
        r.add(
            "集中度",
            post_pct <= cap,
            f"成交后该标的占股票市值 {post_pct:.1f}%(上限 {cap:.0f}%)",
            hint="加仓后会超过单一标的上限 —— 减小尺寸或换标的。" if post_pct > cap else "",
        )

    if total is not None and bp is not None:
        total = float(total)
        floor = float(cfg.get("cash", {}).get("floor_pct", 15))
        post_bp_pct = (bp - amount) / total * 100 if total > 0 else 0
        r.add(
            "现金底线",
            post_bp_pct >= floor,
            f"成交后 BP 占总值 {post_bp_pct:.1f}%(底线 {floor:.0f}%)",
            hint="会击穿现金底线 —— 减小尺寸。" if post_bp_pct < floor else "",
        )


def rule_size_tier(rule, summary: dict, ex: dict) -> tuple[float, str]:
    """一条规则允许多大的尺寸系数,以及理由。

    证据强度决定**仓位大小**,不决定能不能交易 —— 否则第一天就死锁:
    没有数据 → 不许交易 → 永远攒不到验证规则所需的数据。
    """
    from rules import MODERATE, STRONG, SUPPORTS, WEAK, audit_rule

    observe = float(ex.get("size_scale_observe", 0.4))
    weak = float(ex.get("size_scale_weak", 0.7))
    supported = float(ex.get("size_scale_supported", 1.0))

    if rule.scope == "none":
        return 0.0, f"{rule.id} 已停用({rule.status})"
    if rule.kind == "process":
        return supported, f"{rule.id} 流程纪律"

    v = audit_rule(rule, summary)
    if v.result == SUPPORTS and v.confidence in (MODERATE, STRONG):
        return supported, f"{rule.id} 数据支持({v.samples} 事件)"
    if v.result == SUPPORTS and v.confidence == WEAK:
        return weak, f"{rule.id} 弱支持({v.samples} 事件)"
    if rule.status == "supported":
        return supported, f"{rule.id} 你已批准升为 supported"
    return observe, f"{rule.id} 观察期({v.samples} 事件)"


def check_rule_scope_and_size(r: Result, cfg: dict, order: dict) -> None:
    """按依据规则的证据强度自动缩放单笔上限。

    · 未声明 rule_ids → 按最低档(无法核实依据,就不能给更大尺寸)
    · 声明多条 → 取其中**最强**的一条(引用额外的弱上下文不该受罚)
    · 引用已停用(refuted / retired)的规则 → 直接拒绝
    """
    ex = cfg.get("execution", {})
    observe_scale = float(ex.get("size_scale_observe", 0.4))
    cap = float(ex.get("max_order_usd", 80))
    amount = _order_amount(order) or 0.0
    ids = [str(x) for x in (order.get("rule_ids") or [])]

    if not ids:
        allowed = cap * observe_scale
        r.add(
            "依据强度与尺寸",
            amount <= allowed,
            f"未声明 rule_ids → 按最低档 ×{observe_scale:g}:上限 ${allowed:.2f}"
            f"(本笔 ${amount:.2f})",
            hint=f"把金额降到 ${allowed:.2f} 以内即可放行;"
                 f"或在订单里声明 rule_ids —— 依据已被数据支持的规则可以拿更大尺寸。",
        )
        return

    try:
        from rules import load_rules
        from stats import summarize

        rules, _, _ = load_rules()
        summary = summarize(parse_trades())
    except Exception as exc:                      # noqa: BLE001
        r.add("依据强度与尺寸", False, f"无法加载规则或台账:{exc}", fatal=False)
        return

    by_id = {x.id: x for x in rules}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        r.add("依据强度与尺寸", False, f"引用了不存在的规则 id:{unknown}",
              hint="检查 config/rules.toml,或跑 make rules-check。")
        return

    tiers = [rule_size_tier(by_id[i], summary, ex) for i in ids]
    dead = [why for scale, why in tiers if scale == 0.0]
    if dead:
        r.add("依据强度与尺寸", False, f"引用了已停用的规则:{dead}",
              hint="refuted / retired 的规则只保留历史,不参与决策。")
        return

    best_scale, best_why = max(tiers, key=lambda x: x[0])
    allowed = cap * best_scale
    r.add(
        "依据强度与尺寸",
        amount <= allowed,
        f"最强依据 {best_why} → ×{best_scale:g},上限 ${allowed:.2f}(本笔 ${amount:.2f})",
        hint=f"把金额降到 ${allowed:.2f} 以内即可放行 —— "
             f"仓位随证据积累自动放大,不需要人工调整。"
        if amount > allowed
        else "",
    )


def check_reason_tag(r: Result, order: dict, vocab=None) -> None:
    vocab = vocab or load_vocabulary()
    tag = order.get("reason_tag", "")
    side = str(order.get("side", "")).lower()
    expected = vocab.expected_for(side)
    r.add(
        "理由标签",
        tag in expected,
        f"{tag!r}({side} 可用:{'/'.join(expected)})",
        hint=f"用 {vocab.source} 里定义的标签,不要自创 —— 台账统计按标签归集。"
        if tag not in expected
        else "",
    )


def check_daily_limits(r: Result, cfg: dict, order: dict) -> None:
    ex = cfg.get("execution", {})
    today = order.get("today_orders") or []
    amount = _order_amount(order) or 0.0
    side = str(order.get("side", "")).lower()

    same_side = [o for o in today if str(o.get("side", "")).lower() == side]
    used = sum(float(o.get("amount", o.get("amount_usd", 0)) or 0) for o in same_side)
    cap_daily = float(ex.get("max_daily_usd", 200))
    r.add(
        f"单日累计({side})",
        used + amount <= cap_daily,
        f"已用 ${used:.2f} + 本笔 ${amount:.2f} = ${used + amount:.2f} / 上限 ${cap_daily:.2f}",
    )

    cap_n = int(ex.get("max_orders_per_day", 6))
    r.add(
        "单日笔数",
        len(today) + 1 <= cap_n,
        f"今日已 {len(today)} 笔 + 本笔 = {len(today) + 1} / 上限 {cap_n}",
    )

    # --- 同一标的当日不重复操作
    sym = str(order.get("symbol", "")).upper()
    dup = [o for o in today if str(o.get("symbol", "")).upper() == sym]
    r.add(
        "同标的当日重复",
        not dup,
        f"{sym} 今日已有 {len(dup)} 笔操作" if dup else f"{sym} 今日尚无操作",
        hint="同一标的当日不重复操作是硬规则。若用户明确要求追加,需人工确认覆盖。"
        if dup
        else "",
    )


def check_ref_id(r: Result, order: dict) -> None:
    ref = str(order.get("ref_id", "")).strip()
    if not ref:
        r.add(
            "ref_id",
            False,
            "缺失",
            hint="每笔用全新 UUID(`uuidgen`);只有重试同一笔才复用同一个 ref_id(幂等)。",
        )
        return
    if not UUID_RE.match(ref):
        r.add("ref_id", False, f"{ref!r} 不是合法 UUID 格式")
        return

    # 去重:先查本次传入的当日订单,再尽力扫台账 note 列
    today = order.get("today_orders") or []
    if any(str(o.get("ref_id", "")).lower() == ref.lower() for o in today):
        r.add("ref_id 未重复", False, f"{ref[-8:]} 已出现在今日订单中",
              hint="复用 ref_id 只允许用于重试同一笔。若确为重试,人工确认后覆盖。")
        return

    seen_in_ledger = False
    try:
        for t in parse_trades():
            if ref.lower() in (t.note or "").lower():
                seen_in_ledger = True
                break
    except LedgerError:
        pass  # 台账格式问题由 stats/doctor 报,这里不重复报

    r.add(
        "ref_id 未重复",
        not seen_in_ledger,
        f"{ref[-8:]} 未在今日订单与台账中出现" if not seen_in_ledger else f"{ref[-8:]} 已在台账中",
        hint="去重范围仅限本次传入的 today_orders + 台账 note 列 —— 完整去重需要台账"
        "增加 order_id 列(尚未实施),所以这不是完备保证。",
    )


# ------------------------------------------------------------------------ 主流程
def run(order: dict, cfg: dict, now: dt.datetime | None = None, vocab=None) -> Result:
    now = now or now_et()
    vocab = vocab or load_vocabulary()
    r = Result()

    check_kill_switch(r, cfg)
    check_execution_enabled(r, cfg)
    check_account(r, cfg, order)
    check_market_session(r, cfg, order, now)
    check_intent_freshness(r, cfg, order, now)
    check_quote_timestamp(r, cfg, order, now)
    check_reason_tag(r, order, vocab)
    check_rule_scope_and_size(r, cfg, order)
    check_size(r, cfg, order)
    check_concentration_and_bp(r, cfg, order)
    check_daily_limits(r, cfg, order)
    check_ref_id(r, order)

    if r.blockers:
        r.verdict = DENY
    elif bool(cfg.get("execution", {}).get("dry_run", True)):
        r.verdict = DRY_RUN
    else:
        r.verdict = ALLOW
    return r


def print_report(r: Result, cfg: dict, order: dict) -> None:
    side = "买入" if str(order.get("side")).lower() == "buy" else "卖出"
    amt = _order_amount(order)
    print(f"=== preflight · {side} {order.get('symbol')} "
          f"{('$%.2f' % amt) if amt is not None else ''} ===\n")

    for c in r.checks:
        icon = "✅" if c.ok else ("❌" if c.fatal else "⚠️ ")
        print(f"{icon} {c.name}:{c.detail}")
        if c.hint:
            print(f"      → {c.hint}")

    print()
    if r.verdict == DENY:
        print(f"❌ DENY —— {len(r.blockers)} 项硬约束未通过,禁止下单。")
        print("   preflight 返回 DENY 就是不许下,不得绕过或「人工判断通过」。")
    elif r.verdict == DRY_RUN:
        print("🧪 DRY_RUN —— 所有硬约束通过,但 execution.dry_run = true。")
        print("   走完 review 并输出「本应下什么单」,不要调用 place_equity_order。")
    else:
        print("✅ ALLOW —— 所有硬约束通过,可以 review → place。")
        if bool(cfg.get("execution", {}).get("require_confirmation", True)):
            print("   注意 require_confirmation = true:仍需等用户明确确认后才 place。")
    if r.warnings:
        print(f"\n⚠️  {len(r.warnings)} 项提醒(不阻断,但要在汇报里说明)。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下单前置确定性检查")
    # 注意:输入源不能放进 required=True 的互斥组 —— 那样 `--example` 单独用会被
    # argparse 先拦下(它并不需要输入源)。改为手动校验。
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--stdin", action="store_true", help="从 stdin 读订单 JSON")
    src.add_argument("--order-file", help="订单 JSON 文件路径")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--now-et", help="覆盖当前 ET 时间(测试用),YYYY-MM-DD HH:MM")
    ap.add_argument("--tags", help="指定理由标签词表文件(演示/测试用,默认读 config/)")
    ap.add_argument("--example", action="store_true", help="打印订单 JSON 模板并退出")
    args = ap.parse_args(argv)

    if args.example:
        print(json.dumps(EXAMPLE_ORDER, ensure_ascii=False, indent=2))
        return 0

    if not args.stdin and not args.order_file:
        ap.error("需要 --stdin 或 --order-file 之一(或用 --example 看模板)")

    raw = sys.stdin.read() if args.stdin else Path(args.order_file).read_text(encoding="utf-8")
    try:
        order = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"❌ 订单不是合法 JSON:{exc}", file=sys.stderr)
        return 2

    errs = validate_order(order)
    if errs:
        print("❌ 订单字段有问题:", file=sys.stderr)
        for e in errs:
            print(f"  · {e}", file=sys.stderr)
        print("\n模板:python3 scripts/preflight.py --example", file=sys.stderr)
        return 2

    cfg = load_config("profile", required=False)
    try:
        now = _parse_et(args.now_et, "--now-et") or now_et()
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    vocab = load_vocabulary(path=Path(args.tags)) if args.tags else None
    r = run(order, cfg, now, vocab=vocab)

    if args.json:
        print(json.dumps(
            {
                "verdict": r.verdict,
                "config_source": cfg.get("_source"),
                "config_is_example": cfg.get("_is_example"),
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail,
                     "fatal": c.fatal, "hint": c.hint}
                    for c in r.checks
                ],
                "blockers": [c.name for c in r.blockers],
                "warnings": [c.name for c in r.warnings],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        if cfg.get("_is_example"):
            print("⚠️  config/profile.toml 不存在,当前用样例配置 —— 结论仅供演示。\n")
        print_report(r, cfg, order)

    return 1 if r.verdict == DENY else 0


EXAMPLE_ORDER = {
    "account_id": "000000000",
    "symbol": "AAAA",
    "side": "sell",
    "amount_usd": 30.0,
    "order_type": "market",
    "market_hours": "regular_hours",
    "intent": "partial",
    "reason_tag": "减仓",
    "ref_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    "analysis_at_et": "2026-08-18 13:01",
    "quote_timestamp_et": "2026-08-18 13:02",
    "position": {"market_value": 120.0, "qty": 0.3, "avg_cost": 390.0},
    "portfolio": {"total_value": 500.0, "buying_power": 90.0,
                  "cash": 90.0, "equity_value": 410.0},
    "today_orders": [],
}


if __name__ == "__main__":
    sys.exit(main())
