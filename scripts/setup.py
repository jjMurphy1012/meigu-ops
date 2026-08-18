#!/usr/bin/env python3
"""首次接入的状态机 —— 连接要早、只读;授权要晚、单独。

用法:
    make setup                      # 看当前处于哪一步、下一步做什么
    python3 scripts/setup.py --json
    python3 scripts/setup.py --record-mcp --stdin      # agent 提交只读验证结果
    python3 scripts/setup.py --record-drill --stdin    # agent 提交 dry-run 演练结果
    python3 scripts/setup.py --authorize-live guarded --approved

============================================================================
为什么要状态机
============================================================================
这个项目的核心是"AI + 券商 MCP 自动交易"。如果把连接券商放在最后,新用户会
配置半天才发现账户、权限或 MCP 根本不可用 —— 而那时 MCP 的问题和项目配置的
问题已经混在一起,分不清是谁的错。

但**连接成功 ≠ 获得真钱执行权限**。所以顺序是:

    先只读连上 → 再配账户与上限 → 再写自己的策略 → 再演练 → 最后单独授权

============================================================================
脚本与 agent 的分工
============================================================================
MCP 工具只有 agent 能调,Python 脚本调不了。所以:

  · **脚本**拥有状态判定、结果校验、以及"能不能进入下一步"的裁决
  · **agent**执行 MCP 只读调用与 dry-run 演练,把结果经 `--record-*` 写回

写回的内容会被逐项校验;缺项、账户号对不上、或验证时执行开关是开着的,
都会被拒绝 —— 这样"验证通过"就不是 agent 的一句自我声明。

状态记录在 `data/setup-state.json`(用户层,已 gitignore)。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from meigu_lib import CONFIG_DIR, DATA_DIR, ROOT, ConfigError, now_et

STATE_FILE = DATA_DIR / "setup-state.json"

UNINITIALIZED = "UNINITIALIZED"
MCP_CONNECTED_READONLY = "MCP_CONNECTED_READONLY"
PROFILE_READY = "PROFILE_READY"
STRATEGY_READY = "STRATEGY_READY"
AUTOMATION_READY = "AUTOMATION_READY"
LIVE_AUTHORIZED = "LIVE_AUTHORIZED"

ORDER = [UNINITIALIZED, MCP_CONNECTED_READONLY, PROFILE_READY,
         STRATEGY_READY, AUTOMATION_READY, LIVE_AUTHORIZED]

PLACEHOLDER_ACCOUNTS = {"", "000000000", "111111111", "123456789", "987654321"}

# agent 提交只读验证时必须逐项给出结论。缺一项就不算通过 ——
# "检测到 MCP 配置文件"不是验收标准,**真的读到数据**才是。
MCP_CHECKS = {
    "accounts_listed": "能读取账户列表",
    "target_account_found": "能在其中找到配置指定的子账户",
    "target_account_unique": "该账户号在列表中唯一(没有歧义)",
    "positions_readable": "能读取持仓",
    "buying_power_readable": "能读取 buying power",
    "quote_with_timestamp": "能获得**带时间戳**的报价(时间戳熔断依赖它)",
    "review_order_available": "review_equity_order 可调用(只审不下)",
    "place_order_not_called": "本次验证全程**未**调用 place_equity_order",
    "fail_closed_on_error": "账户缺失 / 账户不符 / 连接中断时均 fail-closed(不猜、不继续)",
}

DRILL_CHECKS = {
    "premarket_ran": "盘前分析跑通,产出候选与防守预案",
    "check_ran": "盘中检查点跑通,给出买/卖/不动结论",
    "preflight_ran": "preflight 跑通并返回明确判定",
    "order_simulated": "在 dry_run 下走完 review,未真实下单",
    "journal_written": "尾盘日志与台账写入成功",
    "review_ran": "复盘审计跑通,规则拿到判定",
}


@dataclass
class Step:
    state: str
    ok: bool
    detail: str
    todo: list[str] = field(default_factory=list)


def _read_profile() -> dict:
    """直接读 CONFIG_DIR 下的 profile.toml。

    不走 load_config():那个函数有自己的路径解析(会回退到 .example),
    与本模块的 CONFIG_DIR 可能指向不同目录 —— 状态判定必须只看一个来源。
    """
    import tomllib

    path = CONFIG_DIR / "profile.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:                             # noqa: BLE001
        return {}


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------------ 各步判定
def check_uninitialized() -> Step:
    todo = []
    v = sys.version_info
    if (v.major, v.minor) < (3, 11):
        todo.append(f"升级 Python 到 3.11+(当前 {v.major}.{v.minor})")
    for f in ("AGENTS.md", "modes/_mechanics.md", "scripts/preflight.py"):
        if not (ROOT / f).exists():
            todo.append(f"仓库文件缺失:{f}")
    return Step(UNINITIALIZED, not todo,
                "环境与仓库完整性" if not todo else "环境有问题", todo)


def check_mcp(state: dict) -> Step:
    rec = state.get("mcp_check") or {}
    if not rec.get("passed"):
        return Step(
            MCP_CONNECTED_READONLY, False, "尚未完成券商 MCP 只读验证",
            ["跑 `/meigu-ops setup`,由 agent 执行只读验证并写回结果",
             "只读:读账户 / 持仓 / 买力 / 带时间戳的报价 / review 可用",
             "**不要**调用 place_equity_order —— 这一步只确认能连上、连对账户"],
        )
    return Step(MCP_CONNECTED_READONLY, True,
                f"已验证(账户 ***{rec.get('account_last4', '????')},"
                f"{rec.get('at', '')})")


def check_profile() -> Step:
    path = CONFIG_DIR / "profile.toml"
    if not path.exists():
        return Step(PROFILE_READY, False, "config/profile.toml 不存在",
                    ["cp config/profile.example.toml config/profile.toml",
                     "填入验证过的子账户号,并设定单笔/单日/笔数上限与 kill switch"])
    cfg = _read_profile()
    if not cfg:
        return Step(PROFILE_READY, False, "config/profile.toml 无法解析", ["修好 TOML 语法"])
    todo = []
    acct = str(cfg.get("account", {}).get("id", "")).strip()
    if acct in PLACEHOLDER_ACCOUNTS:
        todo.append("account.id 仍是占位值 —— 填入你自己验证过的子账户号")
    ex = cfg.get("execution", {})
    for k in ("max_order_usd", "max_daily_usd", "max_orders_per_day", "kill_switch_file"):
        if not ex.get(k):
            todo.append(f"execution.{k} 未设置")
    return Step(PROFILE_READY, not todo,
                f"账户 ***{acct[-4:]} · 单笔 ${ex.get('max_order_usd')} / "
                f"单日 ${ex.get('max_daily_usd')} / {ex.get('max_orders_per_day')} 笔"
                if not todo else "配置不完整", todo)


def check_strategy() -> Step:
    todo = []
    for f, hint in (
        ("config/reason-tags.toml", "cp config/reason-tags.example.toml config/reason-tags.toml"),
        ("config/rules.toml", "cp config/rules.example.toml config/rules.toml"),
        ("modes/_strategy.md", "cp modes/_strategy.example.md modes/_strategy.md"),
    ):
        if not (ROOT / f).exists():
            todo.append(f"{f} 不存在 —— {hint}")
    if todo:
        return Step(STRATEGY_READY, False, "策略层尚未建立", todo)

    try:
        from rules import load_rules

        rules, _, _ = load_rules()
    except Exception as exc:                      # noqa: BLE001
        return Step(STRATEGY_READY, False, f"规则文件有问题:{exc}", ["跑 make rules-check"])

    market = [r for r in rules if r.kind == "market"]
    if not market:
        return Step(
            STRATEGY_READY, False, "还没有任何市场判断类规则",
            ["回答 modes/_strategy.example.md 里的问题,把答案写成 [[rule]]",
             "**本仓库不提供答案** —— 没有市场规则时只能跑分析与 dry-run,不能下真单"],
        )
    return Step(STRATEGY_READY, True,
                f"{len(rules)} 条规则({len(market)} 条市场判断)")


def check_automation(state: dict) -> Step:
    rec = state.get("drill") or {}
    if not rec.get("passed"):
        return Step(
            AUTOMATION_READY, False, "尚未完成 dry-run 端到端演练",
            ["确认 execution.dry_run = true",
             "跑一遍 盘前 → 盘中 → preflight → 模拟下单 → 尾盘日志 → 复盘",
             "由 agent 用 `setup.py --record-drill` 写回结果"],
        )
    return Step(AUTOMATION_READY, True, f"演练通过({rec.get('at', '')})")


def check_live() -> Step:
    cfg = _read_profile()
    if not cfg:
        return Step(LIVE_AUTHORIZED, False, "配置不可读")
    ex = cfg.get("execution", {})
    if not ex.get("enabled"):
        return Step(LIVE_AUTHORIZED, False, "真钱执行未开启(这是默认且安全的状态)",
                    ["确认前面全部通过后,用 setup.py --authorize-live 开启"])
    mode = str(ex.get("live_mode", "guarded"))
    dry = ex.get("dry_run", True)
    if dry:
        return Step(LIVE_AUTHORIZED, False, "enabled=true 但仍在 dry_run",
                    ["dry_run = false 才会真正提交订单"])
    return Step(LIVE_AUTHORIZED, True,
                f"真钱执行已开启 · live_mode = {mode}"
                + ("(仓位统一按最低档)" if mode == "guarded" else "(仓位按规则状态缩放)"))


def evaluate() -> tuple[list[Step], str]:
    state = _load_state()
    steps = [
        check_uninitialized(),
        check_mcp(state),
        check_profile(),
        check_strategy(),
        check_automation(state),
        check_live(),
    ]
    current = UNINITIALIZED
    for s in steps:
        if not s.ok:
            break
        current = s.state
    return steps, current


# ------------------------------------------------------------------ 写回命令
def record_mcp(payload: dict) -> str:
    """校验并记录券商 MCP 只读验证结果。"""
    missing = [k for k in MCP_CHECKS if not payload.get(k)]
    if missing:
        raise ConfigError(
            "只读验证未通过,以下项没有确认为 true:\n  "
            + "\n  ".join(f"{k} —— {MCP_CHECKS[k]}" for k in missing)
        )

    acct = str(payload.get("account_id", "")).strip()
    if not acct:
        raise ConfigError("必须提供实际读到的 account_id")

    cfg = _read_profile()
    cfg_acct = str(cfg.get("account", {}).get("id", "")).strip()
    if cfg_acct and cfg_acct not in PLACEHOLDER_ACCOUNTS and acct != cfg_acct:
        raise ConfigError(
            f"读到的账户(***{acct[-4:]})与 config/profile.toml 里配置的"
            f"(***{cfg_acct[-4:]})不一致 —— 先确认要用哪个,不要猜。"
        )

    # 只读验证期间执行开关必须是关的,否则"只读"无从谈起
    ex = cfg.get("execution", {})
    if ex.get("enabled") and not ex.get("dry_run"):
        raise ConfigError(
            "验证期间 execution 处于真钱开启状态 —— 只读验证必须在 "
            "enabled=false 或 dry_run=true 下进行。"
        )

    state = _load_state()
    state["mcp_check"] = {
        "passed": True,
        "at": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "account_last4": acct[-4:],
        "checks": {k: True for k in MCP_CHECKS},
        "notes": str(payload.get("notes", "")),
    }
    _save_state(state)
    return f"券商只读验证已记录(账户 ***{acct[-4:]},{len(MCP_CHECKS)} 项全部通过)"


def record_drill(payload: dict) -> str:
    """校验并记录 dry-run 端到端演练结果。"""
    missing = [k for k in DRILL_CHECKS if not payload.get(k)]
    if missing:
        raise ConfigError(
            "演练未通过,以下环节没有确认为 true:\n  "
            + "\n  ".join(f"{k} —— {DRILL_CHECKS[k]}" for k in missing)
        )
    state = _load_state()
    state["drill"] = {
        "passed": True,
        "at": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "checks": {k: True for k in DRILL_CHECKS},
        "notes": str(payload.get("notes", "")),
    }
    _save_state(state)
    return f"dry-run 演练已记录({len(DRILL_CHECKS)} 个环节全部跑通)"


def authorize_live(mode: str) -> str:
    """开启真钱执行。前置状态必须全部就绪。"""
    if mode not in ("guarded", "autonomous"):
        raise ConfigError("live_mode 只能是 guarded 或 autonomous")
    steps, current = evaluate()
    blocked = [s for s in steps[:5] if not s.ok]
    if blocked:
        raise ConfigError(
            "前置步骤未完成,不能开启真钱执行:\n  "
            + "\n  ".join(f"{s.state}: {s.detail}" for s in blocked)
        )

    path = CONFIG_DIR / "profile.toml"
    text = path.read_text(encoding="utf-8")
    import re

    text = re.sub(r"^enabled = .*$", "enabled = true", text, flags=re.M)
    text = re.sub(r"^dry_run = .*$", "dry_run = false", text, flags=re.M)
    if re.search(r"^live_mode = ", text, flags=re.M):
        text = re.sub(r"^live_mode = .*$", f'live_mode = "{mode}"', text, flags=re.M)
    else:
        text = re.sub(r"^dry_run = false$",
                      f'dry_run = false\n# guarded = 仓位统一按最低档;autonomous = 按规则状态缩放\nlive_mode = "{mode}"',
                      text, flags=re.M)
    path.write_text(text, encoding="utf-8")
    return (
        f"真钱执行已开启 · live_mode = {mode}\n"
        f"  仍然无条件生效:单笔/单日金额上限、单日笔数、kill switch\n"
        f"  想立刻全停:touch data/HALTED"
    )


# ---------------------------------------------------------------------- 输出
def print_report(steps: list[Step], current: str) -> None:
    print("=== meigu-ops 接入状态 ===\n")
    for i, s in enumerate(steps, 1):
        icon = "✅" if s.ok else ("▶" if s.state == ORDER[min(ORDER.index(current) + 1,
                                                             len(ORDER) - 1)] else "  ")
        print(f"{icon} {i}. {s.state:<24} {s.detail}")
        for todo in s.todo:
            print(f"        · {todo}")
    print(f"\n当前状态:{current}")

    nxt = next((s for s in steps if not s.ok), None)
    if nxt is None:
        print("全部就绪。")
    else:
        print(f"下一步:{nxt.state}")
    print(
        "\n只想先看看效果、不连券商?跑 `make dashboard-demo` 与 `python3 scripts/stats.py --demo`"
        "\n—— 全套工作流用虚构固件跑一遍,不需要任何账户。"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="首次接入状态机")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record-mcp", action="store_true", help="记录券商只读验证结果")
    ap.add_argument("--record-drill", action="store_true", help="记录 dry-run 演练结果")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON")
    ap.add_argument("--file", help="从文件读 JSON")
    ap.add_argument("--authorize-live", metavar="MODE",
                    help="开启真钱执行:guarded | autonomous(需 --approved)")
    ap.add_argument("--approved", action="store_true",
                    help="声明用户已明确批准开启真钱执行")
    ap.add_argument("--checklist", action="store_true", help="打印验收项清单")
    args = ap.parse_args(argv)

    if args.checklist:
        print("券商只读验证(--record-mcp 必须逐项为 true):")
        for k, v in MCP_CHECKS.items():
            print(f"  {k:<26} {v}")
        print("\ndry-run 演练(--record-drill):")
        for k, v in DRILL_CHECKS.items():
            print(f"  {k:<26} {v}")
        return 0

    try:
        if args.record_mcp or args.record_drill:
            raw = (sys.stdin.read() if args.stdin
                   else Path(args.file).read_text(encoding="utf-8") if args.file else "")
            if not raw.strip():
                print("❌ 需要 --stdin 或 --file 提供 JSON", file=sys.stderr)
                return 2
            payload = json.loads(raw)
            fn = record_mcp if args.record_mcp else record_drill
            print("✅ " + fn(payload))
            return 0

        if args.authorize_live:
            if not args.approved:
                print("❌ 开启真钱执行需要用户明确批准。", file=sys.stderr)
                print("   先向用户说明:将授权什么、上限是多少、怎么紧急停止;"
                      "得到确认后加 --approved 重跑。", file=sys.stderr)
                return 1
            print("✅ " + authorize_live(args.authorize_live))
            return 0
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"❌ 不是合法 JSON:{exc}", file=sys.stderr)
        return 2

    steps, current = evaluate()
    if args.json:
        print(json.dumps(
            {"current": current,
             "steps": [{"state": s.state, "ok": s.ok, "detail": s.detail, "todo": s.todo}
                       for s in steps]},
            ensure_ascii=False, indent=2))
        return 0
    print_report(steps, current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
