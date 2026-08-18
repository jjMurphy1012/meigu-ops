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
import hashlib
import json
import os
import secrets
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from meigu_lib import CONFIG_DIR, DATA_DIR, ROOT, ConfigError, now_et

STATE_FILE = DATA_DIR / "setup-state.json"
SALT_FILE = DATA_DIR / ".setup-salt"
DRILL_LOG = DATA_DIR / "drill-runs.jsonl"

LIVE_MODES = ("guarded", "autonomous")

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


def _salt() -> str:
    """本机盐值。只用于账户指纹,不参与任何加密。

    有盐才能让指纹既能比对、又不可反推 —— 券商账户号的搜索空间太小,
    不加盐的 sha256 等于明文。文件属于用户层,已 gitignore。
    """
    if SALT_FILE.exists():
        s = SALT_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    s = secrets.token_hex(16)
    SALT_FILE.write_text(s + "\n", encoding="utf-8")
    try:
        SALT_FILE.chmod(0o600)
    except OSError:
        pass
    return s


def account_fingerprint(account_id: str) -> str:
    """账户的不可逆指纹。

    ★ 为什么不存后 4 位就够:后 4 位相同的两个账户会被认成同一个,
    而"换了账户但状态没失效"正是这套状态机要防的事。指纹一变,
    只读验证与演练记录**自动失效** —— 不需要谁记得去清理。
    """
    norm = "".join(ch for ch in str(account_id) if ch.isalnum()).lower()
    if not norm:
        return ""
    return hashlib.sha256((_salt() + ":" + norm).encode()).hexdigest()


def current_account_fingerprint() -> str:
    acct = str(_read_profile().get("account", {}).get("id", "")).strip()
    if not acct or acct in PLACEHOLDER_ACCOUNTS:
        return ""
    return account_fingerprint(acct)


def validate_execution(cfg: dict) -> list[str]:
    """执行参数的取值校验 —— 存在不等于合法。

    一个负数上限、一个 0 笔数、一个指向仓外的 kill switch,都会在运行时
    变成"没有上限"。所以这里检查的是**值**,不只是**有没有**。
    """
    ex = cfg.get("execution", {})
    errs: list[str] = []

    for k in ("max_order_usd", "max_daily_usd"):
        v = ex.get(k)
        if v is None:
            errs.append(f"execution.{k} 未设置")
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"execution.{k} 必须是数字,实际 {v!r}")
        elif v <= 0:
            errs.append(f"execution.{k} 必须 > 0,实际 {v}")

    n = ex.get("max_orders_per_day")
    if n is None:
        errs.append("execution.max_orders_per_day 未设置")
    elif not isinstance(n, int) or isinstance(n, bool):
        errs.append(f"execution.max_orders_per_day 必须是整数,实际 {n!r}")
    elif n < 1:
        errs.append(f"execution.max_orders_per_day 必须 >= 1,实际 {n}")

    for k in ("size_scale_observe", "size_scale_weak", "size_scale_supported"):
        v = ex.get(k)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"execution.{k} 必须是数字,实际 {v!r}")
        elif not (0 < v <= 1):
            errs.append(f"execution.{k} 必须落在 (0, 1],实际 {v} —— 尺寸系数不能放大基准上限")

    for k in ("enabled", "dry_run", "require_confirmation"):
        if k in ex and not isinstance(ex[k], bool):
            errs.append(f"execution.{k} 必须是原生布尔值,实际 {ex[k]!r} —— "
                        f'带引号的 "false" 会被当成真')

    # 倍率必须单调不减:observe <= weak <= supported。
    # 否则可以配出"越没证据仓位越大"的倒置关系,而每一项单独看都合法。
    scales = [(k, ex.get(k)) for k in
              ("size_scale_observe", "size_scale_weak", "size_scale_supported")]
    got = [(k, v) for k, v in scales if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for (k1, v1), (k2, v2) in zip(got, got[1:]):
        if v1 > v2:
            errs.append(f"{k1}({v1})> {k2}({v2})—— 证据越强仓位反而越小,倍率关系倒置")

    mode = ex.get("live_mode", "guarded")
    if mode not in LIVE_MODES:
        errs.append(f"execution.live_mode 只能是 {' | '.join(LIVE_MODES)},实际 {mode!r}")

    ks = ex.get("kill_switch_file")
    if not ks:
        errs.append("execution.kill_switch_file 未设置")
    elif not isinstance(ks, str):
        errs.append(f"execution.kill_switch_file 必须是字符串,实际 {ks!r}")
    else:
        pk = Path(ks)
        if pk.is_absolute():
            errs.append("execution.kill_switch_file 必须是仓库内相对路径")
        else:
            try:
                (ROOT / pk).resolve().relative_to(ROOT.resolve())
            except ValueError:
                errs.append(f"execution.kill_switch_file 指向仓库之外:{ks}")
    return errs


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict) -> None:
    """原子写 —— 半截 JSON 会让状态机在下次读取时静默退回空状态。

    `write_text` 中途被打断会留下截断文件;而 `_load_state` 遇到坏 JSON 返回 {},
    于是"验证过"变成"没验证过",或者更糟:配合旧配置变成状态与实盘开关不一致。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE_FILE)


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
    todo = ["跑 `/meigu-ops setup`,由 agent 执行只读验证并写回结果",
            "只读:读账户 / 持仓 / 买力 / 带时间戳的报价 / review 可用",
            "**不要**调用 place_equity_order —— 这一步只确认能连上、连对账户"]
    if rec.get("passed") is not True:
        return Step(MCP_CONNECTED_READONLY, False, "尚未完成券商 MCP 只读验证", todo)

    # ★ 指纹绑定:验证是针对**某一个具体账户**做的,换了账户就得重做。
    # 只存后 4 位不够 —— 后 4 位相同的两个账户会被认成同一个。
    cur = current_account_fingerprint()
    if not cur:
        # ★ 这是"先 MCP、后 profile"的正常中间态,不是失败:
        # 只读验证已经完成,只是还没有 profile 可以绑定。
        # 旧实现把它判为未完成,于是流程倒退回第 2 步 —— 与公开文档的顺序矛盾。
        return Step(MCP_CONNECTED_READONLY, True,
                    f"已验证(账户 ***{rec.get('account_last4', '????')})· "
                    f"**待绑定** —— 下一步把这个账户号填进 config/profile.toml")
    if rec.get("account_fp") != cur:
        return Step(
            MCP_CONNECTED_READONLY, False,
            "配置里的账户与当时验证过的账户不是同一个 —— 只读验证已自动失效",
            ["换账户后必须对**新账户**重新做一遍只读验证",
             "演练记录同样失效(它也绑定在账户指纹上)"],
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
    todo.extend(validate_execution(cfg))
    return Step(PROFILE_READY, not todo,
                f"账户 ***{acct[-4:]} · 单笔 ${ex.get('max_order_usd')} / "
                f"单日 ${ex.get('max_daily_usd')} / {ex.get('max_orders_per_day')} 笔"
                if not todo else "配置不完整", todo)


def check_strategy(rules_path: Path | None = None, vocab_path: Path | None = None) -> Step:
    todo = []
    for f, hint in (
        ("config/reason-tags.toml", "cp config/reason-tags.example.toml config/reason-tags.toml"),
        ("config/rules.toml", "cp config/rules.example.toml config/rules.toml"),
        ("modes/_strategy.md", "cp modes/_strategy.example.md modes/_strategy.md"),
    ):
        if rules_path is not None:
            continue                              # demo 模式:策略层由固件提供
        if not (ROOT / f).exists():
            todo.append(f"{f} 不存在 —— {hint}")
    if todo:
        return Step(STRATEGY_READY, False, "策略层尚未建立", todo)

    try:
        from meigu_lib import load_vocabulary
        from rules import load_rules

        vocab = load_vocabulary(path=vocab_path) if vocab_path else None
        rules, _, _ = load_rules(path=rules_path, vocab=vocab)
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
    if rec.get("passed") is True and rec.get("account_fp") != current_account_fingerprint():
        return Step(AUTOMATION_READY, False,
                    "演练是针对另一个账户做的 —— 已自动失效",
                    ["对当前账户重新跑一遍 dry-run 演练"])
    if rec.get("passed") is not True:
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
    if ex.get("enabled") is not True:
        return Step(LIVE_AUTHORIZED, False, "真钱执行未开启(这是默认且安全的状态)",
                    ["确认前面全部通过后,用 setup.py --authorize-live 开启"])
    mode = str(ex.get("live_mode", "guarded"))
    if mode not in LIVE_MODES:
        return Step(LIVE_AUTHORIZED, False,
                    f"live_mode = {mode!r} 不是合法值 —— fail-closed",
                    [f"只能是 {' | '.join(LIVE_MODES)}"])
    if ex.get("dry_run", True) is not False:
        return Step(LIVE_AUTHORIZED, False, "enabled=true 但仍在 dry_run",
                    ["dry_run = false 才会真正提交订单"])
    return Step(LIVE_AUTHORIZED, True,
                f"真钱执行已开启 · live_mode = {mode}"
                + ("(仓位统一按最低档)" if mode == "guarded" else "(仓位按规则状态缩放)"))


def evaluate(demo: bool = False) -> tuple[list[Step], str]:
    """跑一遍六步判定。

    `demo=True` 时把配置与状态指向 `examples/` 下的虚构固件 ——
    **跑的是同一套判定代码**,只是数据是假的。这样演示 gif 既能展示真实流程,
    又不会把任何真实账户信息录进公开仓(gif 是要提交的)。
    """
    if demo:
        return _evaluate_demo()

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


def _evaluate_demo() -> tuple[list[Step], str]:
    """用 examples/ 固件跑真实判定 —— 停在"演练未做"这一步。

    停在这里是刻意的:新用户最该看见的不是"全绿",而是
    **"真钱执行还没开,因为演练还没跑"** —— 那才是这套顺序的意义。
    """
    ex = ROOT / "examples"
    demo_cfg = {
        "account": {"id": "000000000"},
        "execution": {"enabled": False, "dry_run": True, "max_order_usd": 80,
                      "max_daily_usd": 200, "max_orders_per_day": 6,
                      "kill_switch_file": "data/HALTED", "live_mode": "guarded"},
    }
    fake_state = {"mcp_check": {"passed": True, "at": "2026-08-18 09:04 ET",
                                "account_last4": "0000", "account_fp": "demo"}}

    steps = [
        check_uninitialized(),
        Step(MCP_CONNECTED_READONLY, True,
             f"已验证(账户 ***0000,{fake_state['mcp_check']['at']})"),
        Step(PROFILE_READY, True, "账户 ***0000 · 单笔 $80 / 单日 $200 / 6 笔"
             if not validate_execution(demo_cfg) else "配置不完整"),
        check_strategy(rules_path=ex / "sample-rules.toml",
                       vocab_path=ex / "sample-reason-tags.toml"),
        check_automation({}),
        Step(LIVE_AUTHORIZED, False, "真钱执行未开启(这是默认且安全的状态)",
             ["确认前面全部通过后,用 setup.py --authorize-live 开启"]),
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
    # ★ 严格比 True:字符串 "false" 是非空字符串,truthiness 判定会当成通过。
    # 对抗测试把九项全部提交为 "false",旧实现照样记"验证通过"。
    missing = [k for k in MCP_CHECKS if payload.get(k) is not True]
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
    if ex.get("enabled") is True and ex.get("dry_run") is False:
        raise ConfigError(
            "验证期间 execution 处于真钱开启状态 —— 只读验证必须在 "
            "enabled=false 或 dry_run=true 下进行。"
        )

    state = _load_state()
    state["mcp_check"] = {
        "passed": True,
        "at": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "account_fp": account_fingerprint(acct),
        "account_last4": acct[-4:],
        "checks": {k: True for k in MCP_CHECKS},
        "notes": str(payload.get("notes", "")),
    }
    _save_state(state)
    return f"券商只读验证已记录(账户 ***{acct[-4:]},{len(MCP_CHECKS)} 项全部通过)"


def start_drill() -> str:
    """开一次 dry-run 演练,返回 run id。

    演练是否真的跑过,不能由 agent 报六个布尔值说了算 —— 那是**自证**。
    这里生成一个 run id,后续每次 `preflight.py` 运行都会把带该 id 的证据
    追加进 `data/drill-runs.jsonl`;`--record-drill` 只认这些证据。
    """
    cfg = _read_profile()
    ex = cfg.get("execution", {})
    if ex.get("enabled") is True and ex.get("dry_run") is False:
        raise ConfigError(
            "当前是真钱模式(enabled=true 且 dry_run=false)—— 演练必须在 dry_run 下进行。"
        )
    fp = current_account_fingerprint()
    if not fp:
        raise ConfigError("配置里没有有效账户号,无法开始演练")

    run_id = secrets.token_hex(8)
    state = _load_state()
    state["drill_active"] = {
        "run_id": run_id,
        "nonce": secrets.token_hex(8),
        "started_at": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "started_ts": now_et().timestamp(),
        "account_fp": fp,
    }
    state.pop("drill", None)      # 重开演练即作废上一次的结论
    _save_state(state)
    return run_id


# 哪些环节能拿到**机器证据**,哪些只能靠 agent 自报 —— 必须写清楚,
# 否则"演练通过"会被读成比它实际强的保证。
MACHINE_VERIFIED_STAGES = ("preflight", "journal", "review")


def append_drill_evidence(stage: str, detail: str = "", *,
                          ok: bool = True, **extra) -> bool:
    """由各阶段的脚本调用,为**当前 active run** 追加一行证据。

    ★ 关键约束:run id 不由调用方指定,而是从 state 里读当前 active run。
    旧实现让 preflight 写下订单里自带的任意 run id,于是不跑 `--start-drill`、
    自造一个 id 就能凭空造出证据 —— 证据链的锚点被调用方控制了。
    """
    active = active_drill_run()
    if not active.get("run_id"):
        return False
    rec = {
        "run_id": active["run_id"],
        "nonce": active.get("nonce", ""),
        "account_fp": active.get("account_fp", ""),
        "stage": stage,
        "ok": bool(ok),                 # ★ 环节自己的成败,不是"跑过就算过"
        "at": now_et().strftime("%Y-%m-%d %H:%M:%S ET"),
        "detail": detail,
        **extra,
    }
    # 一次构造完整记录再单次追加。旧实现是"先追加、再读全文改最后一行、整体写回",
    # 并发跑两个脚本时会改错记录、甚至覆盖掉另一个进程刚写的行。
    DRILL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DRILL_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return True


def active_drill_run() -> dict:
    return (_load_state().get("drill_active") or {})


def drill_evidence(run_id: str) -> list[dict]:
    """读 preflight 写下的演练证据。"""
    if not DRILL_LOG.exists() or not run_id:
        return []
    out = []
    for line in DRILL_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("run_id") != run_id:
            continue
        # nonce 与账户指纹必须与当前 active run 一致 ——
        # 光比 run id,一条旧的、或手写的证据行就够用了。
        active = active_drill_run()
        if active.get("nonce") and rec.get("nonce") != active["nonce"]:
            continue
        if active.get("account_fp") and rec.get("account_fp") != active["account_fp"]:
            continue
        out.append(rec)
    return out


def record_drill(payload: dict) -> str:
    """校验并记录 dry-run 端到端演练结果。

    三道校验,缺一不可:
      ① 前置状态(只读验证 + 账户配置 + 自己的策略)确实就绪
      ② 记录时确实处于 dry_run —— 不能在真钱模式下把"演练"记成完成
      ③ 存在**由 preflight 写下的**证据,且判定确实是 DRY_RUN
    第三条是关键:布尔值可以随口填,证据行必须真的跑过闸门才会出现。
    """
    state = _load_state()

    for step in (check_mcp(state), check_profile(), check_strategy()):
        if not step.ok:
            raise ConfigError(f"演练的前置步骤未就绪 —— {step.state}: {step.detail}")

    ex = _read_profile().get("execution", {})
    if ex.get("enabled") is True and ex.get("dry_run") is False:
        raise ConfigError(
            "当前处于真钱模式 —— 在 enabled=true 且 dry_run=false 下跑出来的不是演练。"
        )

    missing = [k for k in DRILL_CHECKS if payload.get(k) is not True]
    if missing:
        raise ConfigError(
            "演练未通过,以下环节没有确认为 true:\n  "
            + "\n  ".join(f"{k} —— {DRILL_CHECKS[k]}" for k in missing)
        )

    active = active_drill_run()
    if not active.get("run_id"):
        # ★ 旧实现:active 不存在时,payload 里随便给个 id 就能进。
        # 于是"从没开始过演练"和"演练做完了"无法区分。
        raise ConfigError(
            "没有进行中的演练 —— 先跑 `python3 scripts/setup.py --start-drill`,"
            "再用它跑一遍完整流程。不能凭 payload 里自带的 run_id 记录结果。"
        )
    run_id = str(payload.get("run_id", "") or active["run_id"]).strip()
    if run_id != active["run_id"]:
        raise ConfigError("提交的 run_id 与进行中的演练不一致")

    fp = current_account_fingerprint()
    if active.get("account_fp") and active["account_fp"] != fp:
        raise ConfigError("演练开始时的账户与当前配置的账户不是同一个 —— 重新开始演练")

    ev = drill_evidence(run_id)
    if not ev:
        raise ConfigError(
            f"找不到 run {run_id} 的演练证据 —— preflight 没有以该 run id 跑过。\n"
            f"在订单 JSON 里加 \"drill_run_id\": \"{run_id}\" 再跑 preflight。"
        )
    dry_runs = [e for e in ev if e.get("stage") == "preflight"
                and e.get("verdict") == "DRY_RUN"]
    if not dry_runs:
        raise ConfigError(
            f"run {run_id} 有 {len(ev)} 条证据,但没有一条是判定为 DRY_RUN 的 preflight —— "
            f"演练要求走完闸门并停在模拟下单。"
        )
    # ★ "跑过"不等于"跑通"。日志结构校验失败也会留下一条 journal 证据,
    # 旧实现只看 stage 名字在不在,于是一份结构损坏的日志照样算演练通过。
    stages = {e.get("stage") for e in ev if e.get("ok") is not False}
    failed = sorted({e.get("stage") for e in ev if e.get("ok") is False}
                    & set(MACHINE_VERIFIED_STAGES))
    if failed:
        raise ConfigError(
            f"以下环节跑过但**没通过**:{'、'.join(failed)}\n"
            f"  先把它们修到通过,再记录演练 —— 演练的意义是证明链路是通的。"
        )
    lack = [s for s in MACHINE_VERIFIED_STAGES if s not in stages]
    if lack:
        raise ConfigError(
            f"以下环节没有留下机器证据:{'、'.join(lack)}\n"
            f"  preflight → 订单里带 drill_run_id 跑一遍\n"
            f"  journal   → 跑 `make journal-check`\n"
            f"  review    → 跑 `make stats`\n"
            f"  (premarket / check 没有对应脚本,只能由你自报 —— "
            f"这一点在记录里会如实标注。)"
        )

    state["drill"] = {
        "passed": True,
        "at": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "run_id": run_id,
        "account_fp": fp,
        "evidence_count": len(ev),
        "machine_verified": sorted(stages & set(MACHINE_VERIFIED_STAGES)),
        "self_reported": ["premarket_ran", "check_ran"],
        "checks": {k: True for k in DRILL_CHECKS},
        "notes": str(payload.get("notes", "")),
    }
    state.pop("drill_active", None)        # 用完即销:同一个 run 不能记两次
    _save_state(state)
    return (f"dry-run 演练已记录(run {run_id} · {len(ev)} 条证据 · "
            f"机器验证:{'、'.join(sorted(stages & set(MACHINE_VERIFIED_STAGES)))} · "
            f"自报:premarket / check)")


def _set_execution_key(text: str, key: str, literal: str) -> str:
    """在 `[execution]` 段内设置一个键 —— 存在则改,不存在则插入。

    旧实现只做正则替换:配置里若没有 `enabled` 这一行,替换命中零次,
    函数照样返回"授权成功" —— **提示成功、实际没开**。这是最坏的一类失败:
    用户以为开了,系统按没开跑;反过来也可能。所以这里必须区分"改"和"插"。
    """
    import re

    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\[execution\]\s*$", ln):
            start = i
            break
    if start is None:
        return text.rstrip("\n") + f"\n\n[execution]\n{key} = {literal}\n"

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^\s*\[", lines[i]):
            end = i
            break

    for i in range(start + 1, end):
        if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {literal}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    lines.insert(start + 1, f"{key} = {literal}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def authorize_live(mode: str) -> str:
    """开启真钱执行。前置状态必须全部就绪,写入结果必须回读验证。"""
    if mode not in LIVE_MODES:
        raise ConfigError(f"live_mode 只能是 {' | '.join(LIVE_MODES)}")
    steps, _ = evaluate()
    blocked = [s for s in steps[:5] if not s.ok]
    if blocked:
        raise ConfigError(
            "前置步骤未完成,不能开启真钱执行:\n  "
            + "\n  ".join(f"{s.state}: {s.detail}" for s in blocked)
        )

    path = CONFIG_DIR / "profile.toml"
    text = path.read_text(encoding="utf-8")
    for key, literal in (("enabled", "true"), ("dry_run", "false"),
                         ("live_mode", f'"{mode}"')):
        text = _set_execution_key(text, key, literal)

    # 解析 → 校验 → 原子落盘 → 回读 → 再校验。任何一步不符就不写、不报成功。
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"写入会产生非法 TOML,已放弃:{exc}") from exc

    ex = parsed.get("execution", {})
    want = {"enabled": True, "dry_run": False, "live_mode": mode}
    bad = {k: ex.get(k) for k, v in want.items() if ex.get(k) != v}
    if bad:
        raise ConfigError(f"写入后的值与预期不符,已放弃:{bad}")

    tmp = path.with_suffix(".toml.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

    after = _read_profile().get("execution", {})
    still_bad = {k: after.get(k) for k, v in want.items() if after.get(k) != v}
    if still_bad:
        raise ConfigError(
            f"回读校验失败,真钱执行**未开启**:{still_bad} —— 请手动检查 config/profile.toml"
        )

    return (
        f"真钱执行已开启 · live_mode = {mode}(已回读校验)\n"
        f"  仍然生效:kill switch 与账户身份(买卖都无条件)、\n"
        f"           单笔/单日金额上限与笔数(买入无条件;卖出与 intent=close 的清仓有豁免,\n"
        f"           但需提供持仓数据)\n"
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
    ap.add_argument("--start-drill", action="store_true",
                    help="开一次 dry-run 演练,打印 run id(preflight 用它写证据)")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON")
    ap.add_argument("--file", help="从文件读 JSON")
    ap.add_argument("--authorize-live", metavar="MODE",
                    help="开启真钱执行:guarded | autonomous(需 --approved)")
    ap.add_argument("--approved", action="store_true",
                    help="声明用户已明确批准开启真钱执行")
    ap.add_argument("--checklist", action="store_true", help="打印验收项清单")
    ap.add_argument("--demo", action="store_true",
                    help="用 examples/ 的虚构固件跑同一套判定(演示/录制用,不读真实配置)")
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
        if args.start_drill:
            rid = start_drill()
            print(f"✅ 演练已开始 · run id = {rid}")
            print(f'   在订单 JSON 里加 "drill_run_id": "{rid}",跑一遍 preflight 即可留下证据。')
            return 0

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

    steps, current = evaluate(demo=args.demo)
    if args.demo:
        print("🧪 demo 模式:数据来自 examples/ 虚构固件,不读你的真实配置。\n")
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
