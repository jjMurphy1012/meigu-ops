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
    "订单结构",
    "配置结构",
    "紧急停止开关",
    "下单授权",
    "接入状态",
    "配置合法性",
    "账户身份",
    "市场时段",
    "意图时效",
    "报价时间戳熔断",
    "理由标签",
    "证据尺寸",
    "单笔金额上限",
    "退出尺寸上限",
    "减仓占比",
    "残值仓",
    "买力充足",
    "集中度",
    "现金底线",
    "单日累计",
    "单日笔数",
    "同标的当日重复",
    "持仓股数",
    "清仓证据",
    "清仓数量吻合",
    "风控数据完整性",
    "台账可读",
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

VALID_ORDER_TYPES = ("market", "limit")
VALID_MARKET_HOURS = ("regular_hours", "extended_hours", "all_day_hours")
VALID_INTENTS = ("", "partial", "close", "open", "add")

# 金额口径不一致的容忍度。券商成交价与报价之间本就有滑点,
# 但 amount_usd 与 qty×price 差出一个数量级只可能是错误或绕过。
AMOUNT_RECONCILE_TOL = 0.05          # 5%


def _num(value, field: str, errs: list[str], *,
         positive: bool = True, allow_none: bool = True) -> float | None:
    """把一个字段解析成有限数,失败就记错误 —— 不抛异常。

    ★ 为什么不用 float() 直接转:`float("oops")` 抛 ValueError,
    而闸门跑到一半抛异常的结果是 **traceback 而不是 DENY**。
    调用方看到的是崩溃,不是"这单不许下" —— 前者容易被当成偶发故障重试。
    bool 也要挡:Python 里 `True` 是数字 1,`enabled = True` 当金额用不会报错。
    """
    if value is None:
        if not allow_none:
            errs.append(f"{field} 缺失")
        return None
    if isinstance(value, bool):
        errs.append(f"{field} 是布尔值,不是数字:{value!r}")
        return None
    if not isinstance(value, (int, float)):
        errs.append(f"{field} 必须是数字,实际 {type(value).__name__}:{value!r}")
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        errs.append(f"{field} 不是有限数:{value!r}")
        return None
    if positive and f <= 0:
        errs.append(f"{field} 必须 > 0,实际 {f}")
        return None
    return f


def _parse_et(value: str | None, field_name: str) -> dt.datetime | None:
    """解析 ET 时间戳。接受 ISO 或 'YYYY-MM-DD HH:MM[:SS]'。"""
    if not value:
        return None
    from zoneinfo import ZoneInfo

    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = dt.datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
            return naive.replace(tzinfo=ZoneInfo("America/New_York"))
        except ValueError:
            continue
    raise ValueError(f"{field_name} 无法解析为 ET 时间:{value!r}")


def validate_order(order: dict) -> list[str]:
    """订单结构与取值的完整校验。

    这一层的存在理由:后面每一道闸门都假设字段是"能算的数"。假设一旦不成立,
    闸门不是拦住订单,而是**崩掉**;而崩溃在自动化链路里等于"这次没检查"。
    """
    errs: list[str] = []
    if not isinstance(order, dict):
        return ["订单必须是 JSON 对象"]

    errs += [f"缺少必填字段 {f}" for f in REQUIRED_FIELDS if not order.get(f)]

    side = str(order.get("side", "")).lower()
    if side not in ("buy", "sell"):
        errs.append(f"side 必须是 buy 或 sell,实际 {order.get('side')!r}")

    order_type = str(order.get("order_type", "market")).lower()
    if order_type not in VALID_ORDER_TYPES:
        errs.append(f"order_type 必须是 {'/'.join(VALID_ORDER_TYPES)},实际 {order_type!r}")
    session = str(order.get("market_hours", "regular_hours")).lower()
    if session not in VALID_MARKET_HOURS:
        errs.append(f"market_hours 必须是 {'/'.join(VALID_MARKET_HOURS)},实际 {session!r}")
    intent = str(order.get("intent", "")).lower()
    if intent not in VALID_INTENTS:
        errs.append(f"intent 必须是 {'/'.join(x or '(空)' for x in VALID_INTENTS)},实际 {intent!r}")

    amt = _num(order.get("amount_usd"), "amount_usd", errs)
    qty = _num(order.get("qty"), "qty", errs)
    price = _num(order.get("price"), "price", errs)
    if amt is None and qty is None:
        errs.append("必须提供 amount_usd 或 qty 之一")

    # ★ 两套尺寸口径必须自洽。
    # 旧实现:只要有 amount_usd 就忽略 qty×price —— 于是
    # {"amount_usd": 1, "qty": 100, "price": 100} 会按 $1 过闸门、按 100 股成交。
    # 这是最严重的绕过路径:所有金额上限一次全废。
    if amt is not None and qty is not None and price is not None:
        implied = qty * price
        if implied > 0 and abs(implied - amt) / implied > AMOUNT_RECONCILE_TOL:
            errs.append(
                f"金额口径不一致:amount_usd=${amt:.2f} 但 qty×price=${implied:.2f}"
                f"(容差 {AMOUNT_RECONCILE_TOL:.0%})—— 两者必须描述同一笔单"
            )

    for key in ("position", "portfolio"):
        v = order.get(key)
        if v is not None and not isinstance(v, dict):
            errs.append(f"{key} 必须是对象,实际 {type(v).__name__}")

    td = order.get("today_orders")
    if td is None:
        # 缺失不能当成"今天还没下过单" —— 那会把当日累计、笔数、同标的重复
        # 三道闸门一起清零。不知道就是不知道,必须显式给 []。
        errs.append("today_orders 缺失 —— 今日已下单情况未知时不得放行(没有就显式给 [])")
    elif not isinstance(td, list):
        errs.append(f"today_orders 必须是数组,实际 {type(td).__name__}")
    else:
        for i, o in enumerate(td):
            if not isinstance(o, dict):
                errs.append(f"today_orders[{i}] 必须是对象")
                continue
            a = o.get("amount", o.get("amount_usd"))
            if a is not None:
                _num(a, f"today_orders[{i}].amount", errs)

    for key in ("position.market_value", "position.qty", "position.avg_cost"):
        head, tail = key.split(".")
        src = order.get(head)
        if isinstance(src, dict) and tail in src:
            _num(src[tail], key, errs, positive=(tail != "avg_cost"))
    pf = order.get("portfolio")
    if isinstance(pf, dict):
        for k in ("buying_power", "total_value", "equity_value", "cash"):
            if k in pf:
                _num(pf[k], f"portfolio.{k}", errs, positive=False)
    return errs


def validate_config(cfg: dict) -> list[str]:
    """执行配置的类型校验 —— 在跑任何数值闸门之前。

    ★ `enabled = "false"` 这种写法在 Python 里是**真**(非空字符串)。
    配置文件里一个多余的引号就能把总开关变成常开,而且没有任何提示。
    """
    errs: list[str] = []
    ex = cfg.get("execution", {})
    if not isinstance(ex, dict):
        return ["execution 必须是对象"]

    for k in ("enabled", "dry_run", "require_confirmation"):
        if k in ex and not isinstance(ex[k], bool):
            errs.append(
                f"execution.{k} 必须是**原生布尔值** true/false,"
                f"实际 {type(ex[k]).__name__}:{ex[k]!r} —— "
                f"带引号的 \"false\" 会被当成真"
            )
    for k in ("max_order_usd", "max_daily_usd", "intent_ttl_minutes",
              "quote_max_age_minutes", "size_scale_observe",
              "size_scale_weak", "size_scale_supported"):
        if k in ex:
            _num(ex[k], f"execution.{k}", errs)
    if "max_orders_per_day" in ex and (
        not isinstance(ex["max_orders_per_day"], int)
        or isinstance(ex["max_orders_per_day"], bool)
    ):
        errs.append(f"execution.max_orders_per_day 必须是整数,实际 {ex['max_orders_per_day']!r}")
    return errs


def is_live(cfg: dict) -> bool:
    """这一单会真的提交到券商吗?"""
    ex = cfg.get("execution", {})
    return ex.get("enabled") is True and ex.get("dry_run", True) is False


def is_emergency_exit(order: dict) -> bool:
    """紧急清仓通道:降低风险的完整退出。

    只认 `side=sell` + `intent=close`。这条通道会豁免笔数与同标的重复限制 ——
    **风控不能变成"进得去出不来"**。但豁免的代价是必须拿出持仓证据
    (见「清仓证据」闸门),否则任何一笔买单都能自称清仓来绕开上限。
    """
    return (str(order.get("side", "")).lower() == "sell"
            and str(order.get("intent", "")).lower() == "close")


def _setup_evaluate():
    """读接入状态。抽成函数是为了让测试能替换掉它。"""
    from setup import evaluate

    return evaluate()


# ----------------------------------------------------------------------- 各闸门
def check_setup_state(r: Result, cfg: dict) -> None:
    """★ 把首次接入状态机接到下单路径上。

    没有这道闸门,状态机只是个提示系统:它能告诉你"你跳过了只读验证和演练",
    却拦不住下一笔真单。而 `[execution]` 是一个可以手改的 TOML —— 靠
    `--authorize-live` 时检查一次是不够的,旧配置、手改、复制别人的配置
    都能绕过那一次检查。**所以要在每一笔真单进闸门时重新验一遍。**

    dry_run 不受此限:演练本身就是第 5 步,要求它先完成第 5 步是死循环。
    """
    if not is_live(cfg):
        r.add("接入状态", True, "非真钱模式 —— 接入状态不作为闸门", fatal=False)
        return
    try:
        steps, _ = _setup_evaluate()
    except Exception as exc:                      # noqa: BLE001
        r.add("接入状态", False, f"无法评估接入状态:{exc}",
              hint="真钱模式必须 fail-closed。跑 `make setup` 看问题出在哪。")
        return
    bad = [s for s in steps if not s.ok]
    r.add(
        "接入状态",
        not bad,
        "六步全部就绪" if not bad else
        "未完成:" + "、".join(f"{s.state}({s.detail})" for s in bad),
        hint="真钱下单要求接入状态机六步全部通过 —— 跑 `make setup` 按提示补齐。"
        if bad else "",
    )


def check_config_valid(r: Result, cfg: dict) -> None:
    """执行参数的取值校验 —— 一个负数上限等于没有上限。"""
    try:
        from setup import validate_execution

        errs = validate_execution(cfg)
    except Exception as exc:                      # noqa: BLE001
        r.add("配置合法性", False, f"无法校验 execution 配置:{exc}")
        return
    r.add("配置合法性", not errs,
          "execution 参数取值合法" if not errs else ";".join(errs),
          hint="修好 config/profile.toml 再下单 —— 非法上限会在运行时静默变成没有上限。"
          if errs else "")


def check_kill_switch(r: Result, cfg: dict) -> None:
    raw = cfg.get("execution", {}).get("kill_switch_file", "data/HALTED")
    try:
        path = ROOT / str(raw)
        rel = path.relative_to(ROOT)
    except (ValueError, TypeError):
        # 指向仓外或类型不对 —— 那这个"急停开关"永远按不下去。
        r.add("紧急停止开关", False, f"kill_switch_file 非法:{raw!r}",
              hint="必须是仓库内的相对路径,否则急停开关形同虚设。")
        return
    exists = path.exists()
    r.add(
        "紧急停止开关",
        not exists,
        f"{rel} 存在 —— 一切下单已禁止" if exists else "未触发",
        hint=f"确认要恢复交易再删除 {rel}。" if exists else "",
    )


def check_execution_enabled(r: Result, cfg: dict) -> None:
    ex = cfg.get("execution", {})
    enabled = ex.get("enabled", False) is True      # 严格比 True,不用 truthiness
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
        # ★ 曾经只是警告 —— 但"无法核实"和"核实通过"绝不是一回事:
        # 下错子账户是不可撤销的,而这道闸门是唯一能挡住它的地方。
        r.add(
            "账户身份",
            False,
            "订单未带 account_id —— 无法核实是否下在正确的子账户",
            hint="在订单里带上 account_id。核实不了就不许下,不是给个警告放行。",
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
    """本笔的美元口径。

    两种口径都给出时取**较大者**:schema 校验已保证二者相差不超过容差,
    所以这里的 max 不是"选一个",而是"万一校验被绕过时倾向更严"。
    旧实现无条件优先 amount_usd —— 那正是 $1 报价、100 股成交的绕过路径。
    """
    errs: list[str] = []
    amt = _num(order.get("amount_usd"), "amount_usd", errs)
    qty = _num(order.get("qty"), "qty", errs)
    price = _num(order.get("price"), "price", errs)
    implied = qty * price if (qty is not None and price is not None) else None
    vals = [v for v in (amt, implied) if v is not None]
    return max(vals) if vals else None


def check_size(r: Result, cfg: dict, order: dict) -> None:
    ex, pos_cfg = cfg.get("execution", {}), cfg.get("position", {})
    amount = _order_amount(order)
    if amount is None:
        r.add("单笔金额", False, "无法确定金额(需要 amount_usd,或 qty + price)")
        return

    side = str(order.get("side", "")).lower()
    cap = float(ex.get("max_order_usd", 80))
    if side == "sell":
        # ★ 退出敞口不受单笔上限约束 —— 卖出的尺寸天然被持仓本身封顶,
        # 用 max_order_usd 卡住会造成"能建仓、不能完整平仓"。
        mv = (order.get("position") or {}).get("market_value")
        if mv:
            bound = float(mv) * 1.02          # 留 2% 余量给价格波动
            r.add("退出尺寸上限", amount <= bound,
                  f"${amount:.2f} / 该仓位市值 ${float(mv):.2f}(+2% 余量)",
                  hint="卖出金额超过持仓市值 —— 检查是不是算错了股数。"
                  if amount > bound else "")
        else:
            r.add("退出尺寸上限", amount <= cap,
                  f"${amount:.2f} / 上限 ${cap:.2f}(订单未提供持仓市值)",
                  hint="订单里带上 position.market_value,完整平仓就不会被单笔上限卡住。"
                  if amount > cap else "")
    else:
        r.add("单笔金额上限", amount <= cap, f"${amount:.2f} / 上限 ${cap:.2f}")

    if amount <= 0:
        r.add("金额为正", False, f"金额必须 > 0,实际 {amount}")

    # --- 持仓股数:卖出不得超过实际持有
    # 金额口径能被价格抵消(持 1 股、报价算低了,金额就可能仍在上限内),
    # 所以股数必须单独比一次。
    if side == "sell":
        held = (order.get("position") or {}).get("qty")
        want = order.get("qty")
        if want is not None and held is None:
            # ★ 旧实现:两边都有 qty 才比。于是"报了股数、不报持仓"直接免检 ——
            # 缺字段又一次变成免检通道。
            r.add("持仓股数", False,
                  f"按股数下卖单({want})但未提供 position.qty —— 无法确认是否超卖",
                  hint="用 get_equity_positions 读到的实际股数填进 position.qty。")
        elif held is not None and want is not None:
            held_f, want_f = float(held), float(want)
            r.add("持仓股数", want_f <= held_f + 1e-9,
                  f"拟卖 {want_f:g} 股 / 实际持有 {held_f:g} 股",
                  hint="卖出股数超过持仓 —— 现金账户会直接拒单或形成裸空。"
                  if want_f > held_f else "")
            if str(order.get("intent", "")).lower() == "close":
                # 清仓声明的是"全部出清"。差得远说明要么意图写错、要么股数算错,
                # 而 intent=close 会豁免笔数与同标的重复 —— 不能让它名不副实。
                close_ok = want_f >= held_f * 0.995
                r.add("清仓数量吻合", close_ok,
                      f"intent=close:拟卖 {want_f:g} / 持有 {held_f:g}",
                      hint="声明清仓却只卖一部分 —— 改成 intent=partial,"
                           "否则等于用清仓通道换取笔数豁免。"
                      if not close_ok else "")

    if is_emergency_exit(order):
        pos = order.get("position") or {}
        has_evidence = pos.get("market_value") is not None or pos.get("qty") is not None
        r.add("清仓证据", has_evidence,
              "已提供持仓数据" if has_evidence else "声明 intent=close 但没有提供 position 数据",
              hint="紧急清仓通道会豁免笔数与同标的重复限制 —— 豁免必须有持仓证据支撑,"
                   "否则任何单子都能自称清仓来绕开上限。"
              if not has_evidence else "")

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

    # ★ 缺字段不是"这道闸门不适用",而是"这道闸门没跑"。
    # 旧实现对 equity/total/position 是 if-not-None 才检查,于是只要少给两个字段,
    # 集中度与现金底线就自动免检 —— 而它们恰恰是买入端最容易出事的两道。
    missing = [name for name, v in (
        ("portfolio.buying_power", bp),
        ("portfolio.total_value", total),
        ("portfolio.equity_value", equity),
        ("position.market_value", position.get("market_value")),
    ) if v is None]
    if missing:
        r.add("风控数据完整性", False,
              "买单缺少风控字段:" + "、".join(missing),
              hint="这些字段决定集中度与现金底线两道闸门能不能算。缺一项就 fail-closed —— "
                   "用 get_portfolio / get_equity_positions 读齐再下。"
                   "(未持仓时 position.market_value 显式填 0)")
        return

    if True:
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


def rule_size_tier(rule, ex: dict) -> tuple[float, str]:
    """一条**市场判断类**规则允许的尺寸系数。

    ★ 由 `status` 决定,不由审计结果决定。
    审计只产生**升级建议**;实际放大尺寸必须经用户 `--set-status --approved`。
    (旧实现让审计结果直接决定尺寸,导致"状态需批准"形同虚设 —— 攒够样本就自动满额。)
    """
    observe = float(ex.get("size_scale_observe", 0.4))
    weak = float(ex.get("size_scale_weak", 0.7))
    supported = float(ex.get("size_scale_supported", 1.0))

    if rule.scope == "none":
        return 0.0, f"{rule.id} 已停用({rule.status})"
    if rule.status == "supported":
        return supported, f"{rule.id} 已获批准为 supported"
    if rule.status == "provisional":
        return weak, f"{rule.id} 已获批准为 provisional"
    return observe, f"{rule.id} 仍是 {rule.status}(未获批准放大尺寸)"


def check_evidence_size(r: Result, cfg: dict, order: dict) -> None:
    """按证据强度约束**新增风险**的尺寸。

    三条设计原则:
    1. **只约束买入。** 卖出是降低风险 —— 证据不足不该妨碍你退出已有敞口,
       否则系统会变成"允许建仓、不允许平仓"。
    2. **只有 `primary_rule_id` 决定尺寸。** 旧实现对多条依据取 max,
       于是"引用一条弱规则 + 附带一条强规则"就能拿到强规则的尺寸。
    3. **只有市场判断类规则参与缩放。** 流程纪律(process/invariant/enforced)
       是安全闸门,不是下单理由 —— 旧实现让它们返回 1.0,agent 引用一条公开的
       流程规则就能绕开 40% 限制。
    """
    ex = cfg.get("execution", {})
    side = str(order.get("side", "")).lower()
    amount = _order_amount(order) or 0.0

    if side == "sell":
        r.add(
            "证据尺寸(仅约束买入)",
            True,
            "卖出降低风险,不受证据强度约束",
            fatal=False,
            hint="退出敞口的尺寸由持仓本身与「减仓占比」闸门约束,不由证据强度约束。",
        )
        return

    observe_scale = float(ex.get("size_scale_observe", 0.4))
    cap = float(ex.get("max_order_usd", 80))

    primary = str(order.get("primary_rule_id", "") or "")
    context = [str(x) for x in (order.get("context_rule_ids") or [])]

    # ★ 契约统一:订单里只认 primary_rule_id + context_rule_ids。
    # 旧字段 `rule_ids` 曾被文档要求、却不被本函数识别 —— 结果是"我明明写了依据",
    # 系统却按"未声明"给 40% 尺寸,而且不报错。静默降档比报错难查得多。
    legacy = order.get("rule_ids")
    if legacy and not primary:
        r.add(
            "证据尺寸",
            False,
            f"订单用了旧字段 rule_ids={legacy} —— 本闸门只认 primary_rule_id",
            hint="改成 primary_rule_id(本笔主要依据的**一条**规则)"
                 " + context_rule_ids(其余依据)。一条规则决定尺寸,不取 max。",
        )
        return

    if not primary:
        allowed = cap * observe_scale
        r.add(
            "证据尺寸",
            amount <= allowed,
            f"未声明 primary_rule_id → 按最低档 ×{observe_scale:g}:上限 ${allowed:.2f}"
            f"(本笔 ${amount:.2f})",
            hint=f"降到 ${allowed:.2f} 以内即可放行;或声明本笔主要依据的那**一条**规则 —— "
                 f"已获批准为 supported 的规则可以拿满额。",
        )
        return

    # ★ fail-closed:算不出证据等级就不许下单。真钱模式不能因为读文件失败而放行。
    try:
        from rules import load_rules

        rules, _, _ = load_rules()
    except Exception as exc:                      # noqa: BLE001
        r.add(
            "证据尺寸",
            False,
            f"无法加载 config/rules.toml,证据等级不可计算:{exc}",
            hint="真钱模式必须 fail-closed。先跑 make rules-check 修好规则文件。",
        )
        return

    by_id = {x.id: x for x in rules}
    unknown = [i for i in [primary, *context] if i and i not in by_id]
    if unknown:
        r.add("证据尺寸", False, f"引用了不存在的规则 id:{unknown}",
              hint="检查 config/rules.toml,或跑 make rules-check。")
        return

    rule = by_id[primary]
    if rule.kind != "market":
        r.add(
            "证据尺寸",
            False,
            f"primary_rule_id 必须是市场判断类规则,{primary} 是 {rule.kind}",
            hint="流程纪律是安全闸门,不是下单理由 —— 不能用它来放大仓位。",
        )
        return

    scale, why = rule_size_tier(rule, ex)
    # guarded_live:刚开真钱的阶段,仓位统一压到最低档,不管规则状态多好。
    # 这样"开了真钱"和"放开仓位"是两个独立决定,可以先只做前一个。
    # 未知值按 guarded 处理:只判断"是不是 autonomous"而不是"是不是 guarded",
    # 否则 live_mode = "invalid" 会落进宽松分支拿到满尺寸(fail-open)。
    # 非法值本身会被「配置合法性」闸门拦下,这里是第二道。
    mode = str(ex.get("live_mode", "guarded"))
    if mode != "autonomous":
        capped = min(scale, observe_scale)
        if capped < scale:
            why += f" · live_mode={mode} 压到 ×{capped:g}"
        scale = capped
    if scale == 0.0:
        r.add("证据尺寸", False, f"主依据已停用:{why}",
              hint="refuted / retired 的规则只保留历史,不参与决策。")
        return

    allowed = cap * scale
    dead_ctx = [by_id[i].id for i in context if i and by_id[i].scope == "none"]
    r.add(
        "证据尺寸",
        amount <= allowed and not dead_ctx,
        f"主依据 {why} → ×{scale:g},上限 ${allowed:.2f}(本笔 ${amount:.2f})"
        + (f";上下文引用了已停用规则 {dead_ctx}" if dead_ctx else ""),
        hint=f"降到 ${allowed:.2f} 以内即可放行 —— 要放大尺寸,先让 review 用数据"
             f"给出升级建议,再由你 --set-status --approved。"
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
    if side == "sell":
        # 退出敞口不受单日金额上限约束(同上:不能让风控阻止你降低风险)
        r.add(f"单日累计({side})", True,
              f"已用 ${used:.2f} + 本笔 ${amount:.2f} —— 卖出不受单日金额上限约束",
              fatal=False)
    else:
        r.add(
            f"单日累计({side})",
            used + amount <= cap_daily,
            f"已用 ${used:.2f} + 本笔 ${amount:.2f} = ${used + amount:.2f} / 上限 ${cap_daily:.2f}",
        )

    cap_n = int(ex.get("max_orders_per_day", 6))
    emergency = is_emergency_exit(order)
    over_n = len(today) + 1 > cap_n
    r.add(
        "单日笔数",
        (not over_n) or emergency,
        f"今日已 {len(today)} 笔 + 本笔 = {len(today) + 1} / 上限 {cap_n}"
        + ("(紧急清仓豁免)" if over_n and emergency else ""),
        fatal=not emergency,
        hint="笔数上限挡不住降低风险的清仓 —— 但仍受账户身份与持仓证据约束。"
        if over_n and emergency else "",
    )

    # --- 同一标的当日不重复操作
    sym = str(order.get("symbol", "")).upper()
    dup = [o for o in today if str(o.get("symbol", "")).upper() == sym]
    r.add(
        "同标的当日重复",
        (not dup) or emergency,
        (f"{sym} 今日已有 {len(dup)} 笔操作" + ("(紧急清仓豁免)" if emergency else ""))
        if dup else f"{sym} 今日尚无操作",
        fatal=not emergency,
        hint="同一标的当日不重复操作是硬规则。"
             "唯一例外是 intent=close 的清仓 —— 早上加过仓不该导致下午无法离场。"
        if dup else "",
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
        for row in parse_trades():
            if ref.lower() in (row.note or "").lower():
                seen_in_ledger = True
                break
    except LedgerError as exc:
        # ★ 曾经是 `pass` —— 台账读不出来,去重就等于没做,而订单照样放行。
        # 幂等保护失效时最坏的后果是重复下单,所以这里必须 fail-closed。
        r.add("台账可读", False, f"台账无法解析,ref_id 去重不可用:{exc}",
              hint="先跑 `make stats` 或 `make doctor` 修好 data/trades.tsv 再下单。")
        return

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

    # ★ 结构先行,数值在后。字段类型不对时后面每一道闸门都可能抛异常 ——
    # 而在自动化链路里,**崩溃不等于拒绝**:调用方看到的是 traceback,
    # 很容易被当成偶发故障重试一次。所以结构不合法就地 DENY,不再往下算。
    order_errs = validate_order(order if isinstance(order, dict) else {})
    cfg_errs = validate_config(cfg if isinstance(cfg, dict) else {})
    if order_errs or cfg_errs:
        for e in order_errs:
            r.add("订单结构", False, e)
        for e in cfg_errs:
            r.add("配置结构", False, e,
                  hint="TOML 的 true/false 不要加引号 —— 带引号的 \"false\" 是非空字符串,"
                       "在 Python 里等于真。")
        r.verdict = DENY
        return r

    check_kill_switch(r, cfg)
    check_execution_enabled(r, cfg)
    check_config_valid(r, cfg)
    check_setup_state(r, cfg)
    check_account(r, cfg, order)
    check_market_session(r, cfg, order, now)
    check_intent_freshness(r, cfg, order, now)
    check_quote_timestamp(r, cfg, order, now)
    check_reason_tag(r, order, vocab)
    check_evidence_size(r, cfg, order)
    check_size(r, cfg, order)
    check_concentration_and_bp(r, cfg, order)
    check_daily_limits(r, cfg, order)
    check_ref_id(r, order)

    if r.blockers:
        r.verdict = DENY
    elif cfg.get("execution", {}).get("dry_run", True) is not False:
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


def write_drill_evidence(order: dict, r: Result) -> None:
    """把本次判定写进演练证据日志。

    ★ run id **不取自订单**。订单里的 `drill_run_id` 只是"我想为演练留证据"
    这个意图;真正写进证据的 id 来自 `data/setup-state.json` 里的 active run。
    旧实现直接采信订单里的 id —— 于是不跑 `--start-drill`、自造一个 id,
    就能凭空造出一条证据。证据链的锚点不能由被验证方指定。

    写失败不阻断下单 —— 记录演练是次要目的,拦截坏单才是主要目的。
    """
    if not str(order.get("drill_run_id", "") or "").strip():
        return
    try:
        from setup import active_drill_run, append_drill_evidence

        active = active_drill_run()
        if not active.get("run_id"):
            return
        if str(order["drill_run_id"]).strip() != active["run_id"]:
            return                       # 对不上就不写,也不报错
        rec = append_drill_evidence(
            "preflight",
            f"{order.get('side')} {order.get('symbol')} → {r.verdict}",
        )
        if rec:
            from setup import DRILL_LOG

            lines = DRILL_LOG.read_text(encoding="utf-8").splitlines()
            last = json.loads(lines[-1])
            last["verdict"] = r.verdict
            last["blockers"] = [c.name for c in r.blockers]
            lines[-1] = json.dumps(last, ensure_ascii=False)
            DRILL_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass


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
    write_drill_evidence(order, r)

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
    # 买入时必填:本笔主要依据的那一条市场判断类规则(只有它决定尺寸)。
    # 其余依据放 "context_rule_ids": [...]。旧字段 rule_ids 已不再被识别。
    "primary_rule_id": None,
    # 演练时填 `setup.py --start-drill` 给的 run id,preflight 会据此写下证据。
    "drill_run_id": None,
}


if __name__ == "__main__":
    sys.exit(main())
