#!/usr/bin/env python3
"""环境自检 —— 盘前第一件事。

用法:
    python3 scripts/doctor.py
    python3 scripts/doctor.py --json

检查的每一项都对应一次真实的故障:时钟漂移、机器休眠、权限弹窗冻结会话、
台账格式坏掉、日志结构错乱。见 modes/_mechanics.md。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from meigu_lib import (
    CONFIG_DIR,
    ET,
    JOURNAL_MD,
    ROOT,
    TRADES_TSV,
    WEEKDAY_ZH,
    ConfigError,
    LedgerError,
    day_info,
    load_config,
    next_trading_day,
    now_et,
    parse_trades,
    today_et,
)

ORDER_TOOLS = (
    "mcp__robinhood-trading__review_equity_order",
    "mcp__robinhood-trading__place_equity_order",
    "mcp__robinhood-trading__cancel_equity_order",
    "mcp__robinhood-trading__get_equity_orders",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    severity: str = "error"  # error | warn | info


@dataclass
class Result:
    checks: list[Check] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.checks.append(Check(*args, **kwargs))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warn"]


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.returncode, (res.stdout + res.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""


def check_python(r: Result) -> None:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    r.add(
        "Python 版本",
        ok,
        f"{v.major}.{v.minor}.{v.micro}",
        "需要 3.11+(tomllib 在标准库里)" if not ok else "",
    )


def check_clock(r: Result) -> None:
    """时钟漂移 —— 2026-07-13 系统时区从 ET 漂到 MDT,全部 cron 错位 2 小时。"""
    local = dt.datetime.now().astimezone()
    et = now_et()
    local_tz = str(local.tzinfo)
    same = local.utcoffset() == et.utcoffset()
    r.add(
        "本机时区 vs ET",
        same,
        f"本机 {local_tz}({local:%H:%M}) / ET({et:%H:%M})",
        "本机时区不是 ET —— 排 cron 时表达式按 ET 目标时刻写,并在每条 prompt 里用 "
        "`TZ=America/New_York date` 核对(_mechanics.md §4)。"
        if not same
        else "",
        severity="warn",
    )


def check_trading_day(r: Result) -> None:
    today = today_et()
    info = day_info(today)
    detail = f"{today}({WEEKDAY_ZH[today.weekday()]})· {info.reason}"
    if info.is_trading_day:
        detail += f" · 收盘 {info.close_et} ET"
    else:
        detail += f" · 下一交易日 {next_trading_day(today)}"
    r.add("交易日", True, detail, severity="info")


def check_configs(r: Result) -> None:
    for name in ("profile", "watchlist"):
        real = CONFIG_DIR / f"{name}.toml"
        exists = real.exists()
        r.add(
            f"config/{name}.toml",
            exists,
            "已配置" if exists else "缺失,当前会退回 .example",
            f"cp config/{name}.example.toml config/{name}.toml 并填写真实值。"
            "样例里的占位值绝不能用于下单。"
            if not exists
            else "",
            severity="error" if name == "profile" else "warn",
        )


def check_ledger(r: Result) -> None:
    if not TRADES_TSV.exists():
        r.add("data/trades.tsv", True, "尚未创建(还没有交易记录)", severity="info")
        return
    try:
        trades = parse_trades()
    except LedgerError as exc:
        r.add("data/trades.tsv", False, "格式错误", str(exc))
        return
    span = f"{trades[0].date} ~ {trades[-1].date}" if trades else "空"
    r.add("data/trades.tsv", True, f"{len(trades)} 笔 · {span}")


def check_journal(r: Result) -> None:
    if not JOURNAL_MD.exists():
        r.add("data/journal.md", True, "尚未创建", severity="info")
        return
    code, out = _run([sys.executable, str(ROOT / "scripts" / "journal_compress.py"),
                      "--check", "--json"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        r.add("data/journal.md", False, "结构校验脚本执行失败", out[:200])
        return
    detail = f"{data['entry_count']} 条 · {data['total_lines']}/{data['max_lines']} 行"
    r.add(
        "data/journal.md 结构",
        data["ok"],
        detail,
        "; ".join(data["errors"])[:400] if data["errors"] else "",
    )


def check_awake(r: Result) -> None:
    """机器休眠 —— 同一笔减仓曾连续两天在 review 与 place 之间被休眠截杀。"""
    if sys.platform != "darwin":
        r.add("防休眠", True, f"非 macOS({sys.platform}),跳过", severity="info")
        return

    code, _ = _run(["pgrep", "-x", "caffeinate"])
    caffeinate_ok = code == 0
    r.add(
        "caffeinate 常驻",
        caffeinate_ok,
        "运行中" if caffeinate_ok else "未运行",
        "launchctl bootstrap gui/$(id -u) "
        "~/Library/LaunchAgents/com.ustock.keepawake.plist 拉起。"
        "注意:caffeinate 挡不住物理合盖 —— 彻底根治需 `sudo pmset -a disablesleep 1` 或保持开盖。"
        if not caffeinate_ok
        else "",
        severity="warn",
    )

    if shutil.which("pmset"):
        _, batt = _run(["pmset", "-g", "batt"])
        on_ac = "AC Power" in batt
        # ok 必须跟随实际状态:此前恒为 True,导致电池模式显示 ✅ 却又附带警告文案,
        # 而且不计入"提醒"计数 —— 图标与结论自相矛盾。
        r.add(
            "电源",
            on_ac,
            "AC 电源" if on_ac else "电池模式",
            "电池模式闲置 1 分钟即休眠;交易时段请插电。注意 AC 电源也挡不住合盖。"
            if not on_ac
            else "",
            severity="info" if on_ac else "warn",
        )
        _, sleep_cfg = _run(["pmset", "-g"])
        disabled = "SleepDisabled\t\t1" in sleep_cfg or "SleepDisabled  1" in sleep_cfg
        r.add(
            "disablesleep",
            disabled,
            "已开启(合盖也不睡)" if disabled else "未开启(合盖仍会睡)",
            "彻底根治合盖休眠:sudo pmset -a disablesleep 1" if not disabled else "",
            severity="warn",
        )


def check_permissions(r: Result) -> None:
    """策略授权 ≠ 工具放行 —— 未进白名单的工具会弹确认框,冻结整个会话。"""
    path = ROOT / ".claude" / "settings.local.json"
    if not path.exists():
        r.add(
            "下单工具权限白名单",
            False,
            ".claude/settings.local.json 不存在",
            "未放行的 MCP 工具每次调用都弹确认框;用户不在场就无限等待,"
            "整个会话冻结、后续检查点全被阻塞(2026-07-09 中招,损失一整天)。",
            severity="warn",
        )
        return
    try:
        allow = set(json.loads(path.read_text(encoding="utf-8"))
                    .get("permissions", {}).get("allow", []))
    except (json.JSONDecodeError, OSError) as exc:
        r.add("下单工具权限白名单", False, f"解析失败:{exc}")
        return

    missing = [t for t in ORDER_TOOLS if t not in allow]
    r.add(
        "下单工具权限白名单",
        not missing,
        "review/place/cancel/get_orders 四件套齐全"
        if not missing
        else f"缺 {len(missing)} 个:{', '.join(t.split('__')[-1] for t in missing)}",
        "把缺失的工具加进 .claude/settings.local.json 的 permissions.allow。"
        if missing
        else "",
        severity="warn",
    )


def check_execution(r: Result) -> None:
    """下单授权状态 —— 让"现在到底会不会真下单"一眼可见。"""
    try:
        cfg = load_config("profile", required=False)
    except ConfigError as exc:
        r.add("下单授权", False, f"读配置失败:{exc}")
        return
    ex = cfg.get("execution", {})
    enabled = bool(ex.get("enabled", False))
    dry = bool(ex.get("dry_run", True))
    confirm = bool(ex.get("require_confirmation", True))

    if not enabled:
        state = "关闭(不会下任何真单)"
    elif dry:
        state = "开启但 dry_run —— 只走 review,不 place"
    elif confirm:
        state = "开启,每笔需用户确认"
    else:
        state = "⚠️ 开启且自动执行(无逐笔确认)"
    r.add("下单授权", True, state, severity="info")

    if enabled and not dry:
        r.add(
            "下单上限",
            True,
            f"单笔 ${ex.get('max_order_usd', '?')} / 单日 ${ex.get('max_daily_usd', '?')} "
            f"/ {ex.get('max_orders_per_day', '?')} 笔 · 意图 {ex.get('intent_ttl_minutes', '?')}min "
            f"· 报价 {ex.get('quote_max_age_minutes', '?')}min",
            severity="info",
        )

    kill = ROOT / str(ex.get("kill_switch_file", "data/HALTED"))
    if kill.exists():
        r.add(
            "紧急停止开关",
            False,
            f"{kill.relative_to(ROOT)} 存在 —— preflight 会一律 DENY",
            f"确认要恢复交易再删除它。",
            severity="warn",
        )


def check_privacy(r: Result) -> None:
    code, out = _run([sys.executable, str(ROOT / "scripts" / "check_privacy.py")])
    ok = code == 0
    first = next((l.strip() for l in out.splitlines() if l.strip().startswith("·")), "")
    r.add(
        "隐私检查",
        ok,
        "未发现用户层数据泄漏" if ok else "发现问题",
        f"跑 `make check-privacy` 看详情。首个问题:{first}" if not ok else "",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="meigu-ops 环境自检")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    r = Result()
    for fn in (
        check_python,
        check_clock,
        check_trading_day,
        check_configs,
        check_execution,
        check_ledger,
        check_journal,
        check_awake,
        check_permissions,
        check_privacy,
    ):
        fn(r)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not r.failed,
                    "checks": [
                        {
                            "name": c.name,
                            "ok": c.ok,
                            "detail": c.detail,
                            "hint": c.hint,
                            "severity": c.severity,
                        }
                        for c in r.checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not r.failed else 1

    print("=== meigu-ops doctor ===\n")
    for c in r.checks:
        if c.ok:
            icon = "ℹ️ " if c.severity == "info" else "✅"
        else:
            icon = "❌" if c.severity == "error" else "⚠️ "
        print(f"{icon} {c.name}:{c.detail}")
        if c.hint:
            for line in c.hint.split("\n"):
                print(f"      → {line}")

    print()
    if r.failed:
        print(f"❌ {len(r.failed)} 项必须修复,{len(r.warned)} 项提醒。")
        return 1
    if r.warned:
        print(f"⚠️  全部必检项通过,{len(r.warned)} 项提醒 —— 交易时段前建议处理。")
        return 0
    print("✅ 全部检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
