"""交易纪律规则的加载、校验与审计。

规则住在 `config/rules.toml`(已 gitignore)—— **本仓库不提供交易策略**,
它提供的是让你的策略能被台账数据检验、并因此不断修正的机制。

真相源优先级
============
1. **`config/rules.toml` 是执行状态的唯一真相源。** 一条规则当前能不能指导下单,
   只看这里的 `status` 与 `execution_scope`。
2. `modes/_strategy.md` 提供解释、上下文与无法结构化的细节,**不决定执行状态**。
3. `_strategy.md` 里引用规则要用稳定的 `rule.id`。
4. 两者冲突时(例如 rules.toml 已 `refuted` 而散文仍要求执行)——
   **停用该规则并提示用户修正**,不要自行猜哪边是对的。
5. `make rules-check` 检查:缺失/重复 id、未知标签、未知闸门、散文未引用的规则。

三种检验方式
============
    enforced_by   由程序强制(必须是注册表里真实存在的闸门)
    tag_compare   比较若干标签的实现盈亏 → 结论方向 + 证据强度
    manual        无法自动化,复盘时人工过

三类规则(不要混淆)
====================
    invariant  不可关闭的系统安全不变量。无程序把守,靠流程保证。
               **不应该被删除** —— 删掉它并不会让约束消失,只会让它不可见。
    enforced   有程序把守(必须指向注册表里真实存在的闸门)。
               删掉条目不会关闭闸门;要改行为得改 config 或代码。
    market     可配置的风险政策与市场判断,由台账数据裁决状态。

状态机(market 类,由 review 推进,**每次状态变更都需要用户批准**)
==================================================================
    hypothesis ──弱支持──► provisional ──数据支持──► supported
        └────────────────数据推翻────────────────► refuted ──► retired

    ★ 状态**只能由用户批准变更**(`--set-status --approved`)。审计只产生建议。
      仓位尺寸由 status 决定,不由审计结果决定 —— 否则"需要批准"形同虚设:
      攒够样本就自动满额,用户从未同意过。

执行层级(与状态正交)
=====================
    live      可以单独作为下单依据
    observe   可以进入分析,但**不能单独授权真钱下单**
    none      只保留历史,不参与决策

未显式声明时按 status 推导:enforced/supported → live;hypothesis → observe;
refuted/retired → none。**零样本的新假设不应该拿真钱去试。**
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from meigu_lib import (
    CONFIG_DIR,
    ROOT,
    ConfigError,
    load_vocabulary,
    rel_to_root,
    today_et,
)

# ---- 结论方向
SUPPORTS = "supports"
REFUTES = "refutes"
INCONCLUSIVE = "inconclusive"
# ---- 非数据类判定
ENFORCED = "enforced"
MANUAL = "manual"

# ---- 证据强度
INSUFFICIENT = "insufficient"
WEAK = "weak"
MODERATE = "moderate"
STRONG = "strong"

VALID_STATUS = ("invariant", "enforced", "hypothesis", "provisional",
                "supported", "refuted", "retired")
VALID_KIND = ("process", "market")
VALID_TEST_TYPES = ("enforced_by", "tag_compare", "manual")
VALID_SCOPE = ("live", "observe", "none")

# 样本阈值(可被每条规则的 min_samples / weak_min_samples 覆盖)
DEFAULT_WEAK_MIN_SAMPLES = 10
DEFAULT_MIN_SAMPLES = 20

# 状态 → 默认执行层级。零样本假设默认只能观察。
SCOPE_BY_STATUS = {
    "invariant": "live",
    "enforced": "live",
    "supported": "live",
    "hypothesis": "observe",
    "provisional": "live",
    "refuted": "none",
    "retired": "none",
}

RESULT_ZH = {SUPPORTS: "支持", REFUTES: "不支持", INCONCLUSIVE: "数据不足",
             ENFORCED: "程序强制", MANUAL: "需人工核查"}
CONFIDENCE_ZH = {INSUFFICIENT: "样本不足", WEAK: "弱", MODERATE: "中", STRONG: "强"}


def known_gates() -> dict[str, tuple[str, ...]]:
    """闸门注册表 —— `enforced_by` 只能引用这里真实存在的名字。

    写错闸门名会让规则显示"程序强制"但其实无人把守 —— 那比没有这条规则更危险。
    延迟导入 preflight(它不 import 本模块,无循环)。
    """
    gates: dict[str, tuple[str, ...]] = {
        "journal_compress": ("--check",),
        "stats": ("台账校验",),
        "check_privacy": ("隐私检查",),
    }
    try:
        import preflight

        gates["preflight"] = tuple(preflight.GATE_NAMES)
    except Exception:                             # noqa: BLE001
        gates["preflight"] = ()
    return gates


@dataclass
class Rule:
    id: str
    statement: str
    kind: str = "market"
    test: dict = field(default_factory=dict)
    status: str = "hypothesis"
    execution_scope: str = ""          # 空 = 按 status 推导
    min_samples: int | None = None
    weak_min_samples: int | None = None
    evidence: list[str] = field(default_factory=list)
    last_audited: str = ""

    @property
    def test_type(self) -> str:
        return str(self.test.get("type", ""))

    @property
    def scope(self) -> str:
        return self.execution_scope or SCOPE_BY_STATUS.get(self.status, "none")

    @property
    def active(self) -> bool:
        """是否参与决策(含仅观察)。"""
        return self.scope in ("live", "observe")

    @property
    def may_authorize_live(self) -> bool:
        """能否**单独**作为真钱下单的依据。"""
        return self.scope == "live"

    def thresholds(self) -> tuple[int, int]:
        return (
            self.weak_min_samples or DEFAULT_WEAK_MIN_SAMPLES,
            self.min_samples or DEFAULT_MIN_SAMPLES,
        )


@dataclass
class Verdict:
    rule: Rule
    result: str                     # supports | refutes | inconclusive | enforced | manual
    confidence: str = INSUFFICIENT
    detail: str = ""
    suggestion: str = ""
    samples: int = 0

    @property
    def may_change_status(self) -> bool:
        """证据是否强到可以**建议**改状态(仍需用户批准)。"""
        return self.result in (SUPPORTS, REFUTES) and self.confidence in (MODERATE, STRONG)

    @property
    def label(self) -> str:
        if self.result in (ENFORCED, MANUAL, INCONCLUSIVE):
            return RESULT_ZH[self.result]
        prefix = "弱" if self.confidence == WEAK else ""
        return prefix + ("支持" if self.result == SUPPORTS else "反驳")

    @property
    def icon(self) -> str:
        if self.result == ENFORCED:
            return "🔒"
        if self.result == MANUAL:
            return "👁"
        if self.result == INCONCLUSIVE:
            return "…"
        strong = self.confidence in (MODERATE, STRONG)
        if self.result == SUPPORTS:
            return "✅" if strong else "🟢"
        return "❌" if strong else "🟠"


# ------------------------------------------------------------------ 加载与校验
def _validate(rule: Rule, seen: set[str], vocab_tags: set[str],
              gates: dict[str, tuple[str, ...]]) -> list[str]:
    errs: list[str] = []
    if not rule.id:
        errs.append("缺少 id")
    elif rule.id in seen:
        errs.append(f"id 重复:{rule.id!r}(evidence 靠 id 累积,不能重名)")
    if not rule.statement.strip():
        errs.append(f"{rule.id}: statement 为空 —— 规则必须能被读懂才能被证伪")
    if rule.kind not in VALID_KIND:
        errs.append(f"{rule.id}: kind 必须是 {'/'.join(VALID_KIND)},实际 {rule.kind!r}")
    if rule.status not in VALID_STATUS:
        errs.append(f"{rule.id}: status 必须是 {'/'.join(VALID_STATUS)},实际 {rule.status!r}")
    if rule.execution_scope and rule.execution_scope not in VALID_SCOPE:
        errs.append(
            f"{rule.id}: execution_scope 必须是 {'/'.join(VALID_SCOPE)},"
            f"实际 {rule.execution_scope!r}"
        )
    if rule.execution_scope == "live" and rule.status == "hypothesis":
        errs.append(
            f"{rule.id}: 未经数据支持的 hypothesis 不应直接 execution_scope=live。"
            f"先以 observe 跑一段,攒够样本再由 review 升级。"
        )
    if rule.status in ("refuted", "retired") and rule.execution_scope in ("live", "observe"):
        errs.append(
            f"{rule.id}: {rule.status} 的规则不应再参与决策(execution_scope 应为 none)"
        )

    tt = rule.test_type
    if tt not in VALID_TEST_TYPES:
        errs.append(f"{rule.id}: test.type 必须是 {'/'.join(VALID_TEST_TYPES)},实际 {tt!r}")
    elif tt == "tag_compare":
        better = [str(x) for x in rule.test.get("better", [])]
        worse = [str(x) for x in rule.test.get("worse", [])]
        if not better or not worse:
            errs.append(f"{rule.id}: tag_compare 需要 better 与 worse 两组标签")
        unknown = [x for x in better + worse if x not in vocab_tags]
        if unknown:
            errs.append(
                f"{rule.id}: 引用了词表里不存在的标签 {unknown}。"
                f"检查 config/reason-tags.toml —— "
                f"引用不存在的标签会让这条规则永远无法被检验,却看不出哪里错了。"
            )
        if set(better) & set(worse):
            errs.append(f"{rule.id}: better 与 worse 有重叠标签,比较无意义")
    elif tt == "enforced_by":
        by = str(rule.test.get("by", ""))
        if not by:
            errs.append(f"{rule.id}: enforced_by 需要 by(哪道闸门强制)")
        else:
            tool, _, gate = by.partition(":")
            if tool not in gates:
                errs.append(f"{rule.id}: 未知的工具 {tool!r}。可用:{'/'.join(sorted(gates))}")
            elif gates[tool] and gate not in gates[tool]:
                errs.append(
                    f"{rule.id}: {tool} 里没有名为 {gate!r} 的闸门。"
                    f"可用:{'/'.join(gates[tool])} —— "
                    f"写错闸门名会让规则显示『程序强制』但其实无人把守。"
                )
    elif tt == "manual" and not rule.test.get("how"):
        errs.append(f"{rule.id}: manual 需要 how(怎么人工核查)")

    if rule.status == "enforced" and tt != "enforced_by":
        errs.append(
            f"{rule.id}: status=enforced 但 test.type={tt!r}。"
            f"『enforced』表示**有程序把守**;无程序把守的流程纪律用 status=invariant。"
        )
    if rule.status == "invariant":
        if rule.kind != "process":
            errs.append(f"{rule.id}: invariant 只用于 process 类规则")
        if tt == "tag_compare":
            errs.append(
                f"{rule.id}: invariant 是不可协商的流程不变量,不该由盈亏数据裁决"
            )
    return errs


def load_rules(required: bool = False, path: Path | None = None,
               vocab=None) -> tuple[list[Rule], str, bool]:
    """读 config/rules.toml;缺失时回退 .example。返回 (规则, 来源, 是否样例)。"""
    vocab = vocab or load_vocabulary()
    vocab_tags = set(vocab.all)
    gates = known_gates()

    candidates = (
        [(path, True)] if path is not None
        else [(CONFIG_DIR / "rules.toml", False), (CONFIG_DIR / "rules.example.toml", True)]
    )
    for path_, is_example in candidates:
        if path_ is None or not path_.exists():
            continue
        path = path_
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

        rules: list[Rule] = []
        errs: list[str] = []
        seen: set[str] = set()
        for item in raw.get("rule", []):
            rule = Rule(
                id=str(item.get("id", "")),
                statement=str(item.get("statement", "")),
                kind=str(item.get("kind", "market")),
                test=dict(item.get("test") or {}),
                status=str(item.get("status", "hypothesis")),
                execution_scope=str(item.get("execution_scope", "")),
                min_samples=item.get("min_samples"),
                weak_min_samples=item.get("weak_min_samples"),
                evidence=[str(e) for e in (item.get("evidence") or [])],
                last_audited=str(item.get("last_audited", "")),
            )
            errs.extend(_validate(rule, seen, vocab_tags, gates))
            seen.add(rule.id)
            rules.append(rule)

        if errs:
            raise ConfigError(
                f"{rel_to_root(path)} 有 {len(errs)} 处问题:\n  " + "\n  ".join(errs)
            )
        if required and is_example and path.parent == CONFIG_DIR:
            raise ConfigError(
                "config/rules.toml 不存在,当前用的是样例(只含流程纪律,市场判断全空)。\n"
                "  执行:cp config/rules.example.toml config/rules.toml,然后回答里面的问题。\n"
                "  本仓库刻意不提供市场判断类规则 —— 那必须是你自己的。"
            )
        return rules, rel_to_root(path), is_example

    return [], "(未找到 config/rules*.toml)", True


# ------------------------------------------------------------------------ 审计
def _tag_stats(summary: dict, tag: str) -> tuple[float | None, int]:
    """(均笔实现盈亏, 独立决策事件数)。

    ★ 样本单位是**决策事件**(一笔卖出 = 一次退出决策),不是 FIFO lot 数量 ——
    一笔卖单可能匹配三个历史买入批次,那仍然只是一个决策。
    """
    d = (summary.get("by_tag") or {}).get(tag)
    if not d:
        return None, 0
    return d.get("avg_pnl"), int(d.get("events", d.get("count", 0)) or 0)


def audit_rule(rule: Rule, summary: dict) -> Verdict:
    tt = rule.test_type

    if tt == "enforced_by":
        return Verdict(rule, ENFORCED, STRONG, f"由 {rule.test.get('by')} 强制",
                       "审计时确认该闸门仍存在且未被绕过")

    if tt == "manual":
        sug = ("不可关闭的流程不变量 —— 复盘时确认它没有被绕过"
               if rule.status == "invariant" else "复盘时人工过一遍")
        return Verdict(rule, MANUAL, INSUFFICIENT, str(rule.test.get("how", "")), sug)

    if tt != "tag_compare":
        return Verdict(rule, INCONCLUSIVE, INSUFFICIENT, f"未知的 test.type:{tt!r}")

    weak_n, min_n = rule.thresholds()
    better = [str(t) for t in rule.test.get("better", [])]
    worse = [str(t) for t in rule.test.get("worse", [])]

    b = [(t, *_tag_stats(summary, t)) for t in better]
    w = [(t, *_tag_stats(summary, t)) for t in worse]
    b_ok = [(t, v, n) for t, v, n in b if v is not None]
    w_ok = [(t, v, n) for t, v, n in w if v is not None]

    if not b_ok or not w_ok:
        missing = [t for t, v, _ in b + w if v is None]
        return Verdict(rule, INCONCLUSIVE, INSUFFICIENT,
                       f"缺少归集数据的标签:{'/'.join(missing) or '(全部)'}",
                       "继续记录,或检查这些标签是否真的在用")

    def wavg(rows):
        """按决策事件数加权 —— 3 个事件的标签不应与 100 个事件的等权。"""
        total = sum(n for _, _, n in rows)
        if not total:
            return sum(v for _, v, _ in rows) / len(rows)
        return sum(v * n for _, v, n in rows) / total

    b_avg, w_avg = wavg(b_ok), wavg(w_ok)
    n = min(sum(n for _, _, n in b_ok), sum(n for _, _, n in w_ok))

    def fmt(rows):
        return " / ".join(
            f"{t} {'+' if v >= 0 else '-'}${abs(v):.2f}({c} 事件)" for t, v, c in rows
        )

    detail = f"{fmt(b_ok)}  vs  {fmt(w_ok)}"
    result = SUPPORTS if b_avg > w_avg else REFUTES

    if n < weak_n:
        return Verdict(rule, INCONCLUSIVE, INSUFFICIENT,
                       f"{detail} —— 较小一侧仅 {n} 个事件(< {weak_n},方向不可信)",
                       f"攒够 {weak_n} 个决策事件才谈趋势,{min_n} 个才谈改状态", n)
    if n < min_n:
        return Verdict(rule, result, WEAK, f"{detail} —— 较小一侧 {n} 个事件",
                       f"只是趋势提示,不足以改状态(需 ≥{min_n} 个决策事件)", n)

    conf = STRONG if n >= min_n * 2 else MODERATE
    if result == SUPPORTS:
        sug = ("证据足够,可**建议**升为 supported —— 仍需你本人批准"
               if rule.status == "hypothesis" else "")
    else:
        sug = ("数据与规则相反。修正判定标准,或降为 refuted(**保留记录,不要删**)—— "
               "从未被数据支持过的规则是包袱,不是资产")
    return Verdict(rule, result, conf, f"{detail} —— 较小一侧 {n} 个事件", sug, n)


def audit_rules(rules: list[Rule], summary: dict,
                include_inactive: bool = False) -> list[Verdict]:
    return [audit_rule(r, summary) for r in rules if include_inactive or r.active]


# ------------------------------------------------------------------ rules-check
def cross_check_strategy(rules: list[Rule]) -> list[str]:
    """检查散文版 `modes/_strategy.md` 与 rules.toml 的一致性。"""
    path = ROOT / "modes" / "_strategy.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [
        f"{r.id}: 未在 modes/_strategy.md 里被引用 —— "
        f"散文版应以稳定 id 引用规则,否则两边会各自漂移"
        for r in rules
        if r.id and r.id not in text
    ]


# ------------------------------------------------------------------- 写入命令
# ★ 自我进化闭环的最后一环。
#
# 为什么要脚本写而不是让 agent 手改 TOML:手改会破坏格式、丢注释、写错字段名,
# 而这个文件一旦坏掉,整套审计就静默失真 —— 你会以为规则被检验过,其实没有。
#
# 分工:
#   --record-evidence  只追加事实,不改变任何行为 → agent 可自行执行(每日 journal)
#   --set-status       改变规则能否指导下单 → **必须 --approved 显式声明用户已批准**
#   --add-rule         新增假设,默认 hypothesis/observe → agent 可自行执行(复盘产出)

def _rule_block_span(text: str, rule_id: str) -> tuple[int, int]:
    """返回该规则 [[rule]] 块在原文中的起止字符位置。"""
    lines = text.splitlines(keepends=True)
    starts = [i for i, l in enumerate(lines) if l.strip() == "[[rule]]"]
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        block = "".join(lines[i:end])
        if f'id        = "{rule_id}"' in block or f'id = "{rule_id}"' in block:
            return sum(len(x) for x in lines[:i]), sum(len(x) for x in lines[:end])
    raise ConfigError(
        f"找不到 id 为 {rule_id!r} 的规则。跑 `make rules-check` 看现有 id。"
    )


def _value_span(block: str, key: str) -> tuple[int, int]:
    """返回块内 `key = ...` 的值起止位置(支持跨行数组)。"""
    import re as _re

    m = _re.search(rf"^\s*{_re.escape(key)}\s*=\s*", block, _re.M)
    if not m:
        raise ConfigError(f"该规则块里没有 {key} 字段")
    start = m.end()
    rest = block[start:]
    if rest.lstrip().startswith("["):
        depth, i = 0, rest.index("[")
        for j in range(i, len(rest)):
            if rest[j] == "[":
                depth += 1
            elif rest[j] == "]":
                depth -= 1
                if depth == 0:
                    return start, start + j + 1
        raise ConfigError(f"{key} 的数组没有闭合")
    nl = rest.find("\n")
    return start, start + (len(rest) if nl < 0 else nl)


def _toml_str(s: str) -> str:
    """TOML 基本字符串。换行/制表符必须转义 —— 直接写进去会让文件立刻损坏。"""
    return (
        '"'
        + s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        + '"'
    )


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件 → 完整解析校验 → 原子替换。校验失败则原文件纹丝不动。

    自我进化闭环会反复改这个文件。一次写坏就会让整套审计**静默失真** ——
    你会以为规则被检验过,其实文件已经读不出来了。所以每次写入都必须能回滚。
    """
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # 校验:能解析 + 能通过全部规则校验(标签/闸门/状态一致性)
        load_rules(path=Path(tmp))
        os.replace(tmp, path)
    except Exception as exc:                      # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ConfigError(
            f"写入会让 {rel_to_root(path)} 变成非法状态,已回滚(原文件未改动):\n  {exc}"
        ) from exc


def record_evidence(rule_id: str, text: str, path: Path | None = None) -> str:
    """往规则的 evidence 追加一行。只追加事实,不改变行为,无需批准。"""
    path = path or (CONFIG_DIR / "rules.toml")
    raw = path.read_text(encoding="utf-8")
    bs, be = _rule_block_span(raw, rule_id)
    block = raw[bs:be]
    vs, ve = _value_span(block, "evidence")
    current = block[vs:ve].strip()

    if current in ("[]", "[ ]"):
        new_val = f"[{_toml_str(text)}]"
    else:
        inner = current[1:-1].rstrip()
        sep = "" if inner.rstrip().endswith(",") or not inner else ","
        new_val = f"[{inner}{sep} {_toml_str(text)}]"

    updated = block[:vs] + new_val + block[ve:]
    _atomic_write(path, raw[:bs] + updated + raw[be:])
    return f"已为 {rule_id} 追加一条证据"


def set_status(rule_id: str, status: str, note: str = "",
               path: Path | None = None) -> str:
    """改规则状态。**改变的是它能否指导真钱下单**,所以必须用户批准。"""
    if status not in VALID_STATUS:
        raise ConfigError(f"status 必须是 {'/'.join(VALID_STATUS)}")
    path = path or (CONFIG_DIR / "rules.toml")
    raw = path.read_text(encoding="utf-8")
    bs, be = _rule_block_span(raw, rule_id)
    block = raw[bs:be]

    vs, ve = _value_span(block, "status")
    old = block[vs:ve].strip().strip('"')
    block = block[:vs] + _toml_str(status) + block[ve:]

    # 状态与作用域必须同步,两个方向都要:
    #   降级 → 停用(否则"状态说停用、作用域还在跑")
    #   重新启用 → 清掉遗留的 none(否则 refuted→supported 后规则仍然用不了)
    want_scope = "none" if status in ("refuted", "retired") else ""
    try:
        svs, sve = _value_span(block, "execution_scope")
        if want_scope:
            block = block[:svs] + _toml_str(want_scope) + block[sve:]
        else:
            # 清空 = 回到"按 status 推导",避免旧的 none 卡住已恢复的规则
            block = block[:svs] + '""' + block[sve:]
    except ConfigError:
        if want_scope:
            block = block.rstrip("\n") + f'\nexecution_scope = {_toml_str(want_scope)}\n'

    try:
        avs, ave = _value_span(block, "last_audited")
        block = block[:avs] + _toml_str(today_et().isoformat()) + block[ave:]
    except ConfigError:
        pass

    _atomic_write(path, raw[:bs] + block + raw[be:])
    msg = f"{rule_id}: {old} → {status}"
    if note:
        record_evidence(rule_id, f"[{today_et().isoformat()}] {old}→{status}: {note}", path)
    return msg


def add_rule(rule_id: str, statement: str, kind: str = "market",
             test: str = "", path: Path | None = None) -> str:
    """新增一条假设。默认 hypothesis(→ observe),不能直接指导满额下单。"""
    path = path or (CONFIG_DIR / "rules.toml")
    raw = path.read_text(encoding="utf-8")
    try:
        _rule_block_span(raw, rule_id)
        raise ConfigError(f"id {rule_id!r} 已存在")
    except ConfigError as exc:
        if "已存在" in str(exc):
            raise
    if kind not in VALID_KIND:
        raise ConfigError(f"kind 必须是 {'/'.join(VALID_KIND)},实际 {kind!r}")
    test_line = test or '{ type = "manual", how = "复盘时人工核查(待补:怎么查)" }'
    block = f"""

[[rule]]
id        = {_toml_str(rule_id)}
statement = {_toml_str(statement)}
kind      = {_toml_str(kind)}
test      = {test_line}
status    = "hypothesis"
evidence  = []
last_audited = ""
"""
    _atomic_write(path, raw.rstrip("\n") + block)
    return f"已新增 {rule_id}(hypothesis → observe,尺寸按最低档)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验 config/rules.toml")
    ap.add_argument("--file", help="指定规则文件")
    ap.add_argument("--strict", action="store_true", help="散文不同步也算失败")
    ap.add_argument("--record-evidence", nargs=2, metavar=("ID", "TEXT"),
                    help="往某条规则追加一行证据(只记事实,无需批准)")
    ap.add_argument("--set-status", nargs=2, metavar=("ID", "STATUS"),
                    help="改规则状态 —— 需 --approved")
    ap.add_argument("--note", default="", help="配合 --set-status 记录理由")
    ap.add_argument("--approved", action="store_true",
                    help="声明本次状态变更已获用户明确批准")
    ap.add_argument("--add-rule", nargs=2, metavar=("ID", "STATEMENT"),
                    help="新增一条假设(默认 hypothesis → observe)")
    ap.add_argument("--kind", default="market", help="配合 --add-rule")
    args = ap.parse_args(argv)

    target = Path(args.file) if args.file else None
    try:
        if args.record_evidence:
            print("✅ " + record_evidence(*args.record_evidence, path=target))
            return 0
        if args.add_rule:
            print("✅ " + add_rule(args.add_rule[0], args.add_rule[1],
                                  kind=args.kind, path=target))
            return 0
        if args.set_status:
            if not args.approved:
                print("❌ 改状态会改变这条规则能否指导真钱下单 —— 需要用户明确批准。",
                      file=sys.stderr)
                print("   先向用户说明依据与建议,得到确认后再加 --approved 重跑。",
                      file=sys.stderr)
                return 1
            print("✅ " + set_status(args.set_status[0], args.set_status[1],
                                     note=args.note, path=target))
            return 0
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    try:
        rules, src, is_example = load_rules(path=Path(args.file) if args.file else None)
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print(f"=== 规则校验 · {src} ===")
    print(f"共 {len(rules)} 条 · " + " · ".join(
        f"{k} {sum(1 for r in rules if r.status == k)}"
        for k in VALID_STATUS if any(r.status == k for r in rules)))
    print("执行层级:" + " · ".join(
        f"{k} {sum(1 for r in rules if r.scope == k)}"
        for k in VALID_SCOPE if any(r.scope == k for r in rules)))

    obs = [r for r in rules if r.scope == "observe"]
    if obs:
        print(f"\n⚠️  {len(obs)} 条处于观察期,**不能单独作为真钱下单依据**:")
        for r in obs:
            print(f"     · {r.id}:{r.statement[:44]}")

    if not [r for r in rules if r.kind == "market" and r.may_authorize_live]:
        print("\nℹ️  尚无可直接指导下单的市场判断类规则(新装时的正常状态)。")

    warns = cross_check_strategy(rules)
    if warns:
        print(f"\n⚠️  {len(warns)} 条与散文版不同步:")
        for w in warns:
            print(f"     · {w}")

    print("\n✅ 格式、词表引用、闸门引用全部通过。")
    return 1 if (warns and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
