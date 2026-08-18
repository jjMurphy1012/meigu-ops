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
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from meigu_lib import (
    CONFIG_DIR,
    DATA_DIR,
    ROOT,
    ConfigError,
    load_vocabulary,
    now_et,
    rel_to_root,
    today_et,
)

# ---- 结论方向
SUPPORTS = "supports"
# ★ 第三种可能:规则没错,是环境变了。
# 只有 supports / refutes 两个出口时,一次风格切换会被读成"这条规则是错的",
# 于是你在市场刚要回到它擅长的环境时把它退役了。
REGIME_SHIFT = "regime_shift"
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

# 规则是谁写的。
# `ai` 不是"低一等的规则" —— 它走完全相同的生命周期、同样的样本门槛、
# 同样需要你批准才能升级。记 origin 只是为了让复盘时能分开看:
# **我自己写的规则和 AI 代拟的规则,哪一类被数据推翻得更多?**
# 这个问题只有留下出处才答得出来。
VALID_ORIGIN = ("user", "ai")
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
             REGIME_SHIFT: "环境可能变了", ENFORCED: "程序强制", MANUAL: "需人工核查"}
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
    origin: str = "user"               # user = 你自己写的;ai = AI 代拟、你确认过的
    approved_at: str = ""              # origin=ai 时:你确认的时间
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
        if self.result in (ENFORCED, MANUAL, INCONCLUSIVE, REGIME_SHIFT):
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
        if self.result == REGIME_SHIFT:
            return "🔀"
        strong = self.confidence in (MODERATE, STRONG)
        if self.result == SUPPORTS:
            return "✅" if strong else "🟢"
        return "❌" if strong else "🟠"


# --------------------------------------------------------------- 审计历史(稳定性)
AUDIT_LOG = DATA_DIR / "audit-log.jsonl"


def record_audit(verdicts: list["Verdict"]) -> None:
    """把本次审计的判定追加进历史。

    ★ 为什么要留历史:单次判定回答不了"这条规则今天是坏的,下周会不会又变好"。
    **一个会来回翻转的判定,本身就说明它不该被用来改状态。**
    有了历史,"连续两次同向"才算稳定 —— 这是最便宜也最有效的防翻转手段。

    属于用户层(data/ 已 gitignore)。写失败不影响审计本身。
    """
    import datetime as _dt

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _dt.date.today().isoformat()
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            for v in verdicts:
                fh.write(json.dumps({
                    "at": stamp, "rule": v.rule.id, "result": v.result,
                    "confidence": v.confidence, "samples": v.samples,
                }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def audit_history(rule_id: str, limit: int = 5) -> list[dict]:
    """某条规则最近几次的审计判定(旧 → 新)。"""
    if not AUDIT_LOG.exists():
        return []
    out = []
    try:
        for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("rule") == rule_id:
                out.append(rec)
    except OSError:
        return []
    return out[-limit:]


def history_is_stable(rule_id: str, result: str, need: int = 2) -> tuple[bool, str]:
    """最近 `need` 次审计是否都指向同一个结论。

    返回 (是否稳定, 给人看的一句话)。没有历史时视为不稳定 ——
    **第一次看到某个结论,不该立刻据此改变一条规则能否指导真钱下单。**
    """
    hist = [h for h in audit_history(rule_id, limit=need * 3)
            if h.get("result") in (SUPPORTS, REFUTES, REGIME_SHIFT)]
    recent = hist[-need:]
    seq = " → ".join(RESULT_ZH.get(h["result"], h["result"]) for h in recent) or "(无历史)"
    if len(recent) < need:
        return False, f"审计历史只有 {len(recent)} 次(需 {need} 次同向):{seq}"
    if all(h["result"] == result for h in recent):
        return True, f"最近 {need} 次审计一致:{seq}"
    return False, f"最近 {need} 次审计在翻转:{seq} —— 翻转期不要改状态"


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
    if rule.origin not in VALID_ORIGIN:
        errs.append(f"{rule.id}: origin 必须是 {'/'.join(VALID_ORIGIN)},实际 {rule.origin!r}")
    if rule.origin == "ai" and not rule.approved_at:
        # AI 代拟的规则必须留下"你确认过"的时间戳。没有它就分不清
        # "用户选择了信任 AI" 和 "AI 自己往文件里写了一条" —— 那正是要防的。
        errs.append(f"{rule.id}: origin=ai 必须有 approved_at(用户确认的时间)")
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
                origin=str(item.get("origin", "user")),
                approved_at=str(item.get("approved_at", "")),
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
def _tag_events(summary: dict, tag: str) -> list[tuple[str, float]]:
    """某个标签下的逐事件 (日期, 盈亏)。没有明细时回退到均值 × 事件数。"""
    d = (summary.get("by_tag") or {}).get(tag)
    if not d:
        return []
    vals = d.get("values")
    if vals:
        dates = d.get("dates") or [""] * len(vals)
        return list(zip(dates, [float(v) for v in vals]))
    avg, n = d.get("avg_pnl"), int(d.get("events", 0) or 0)
    return [("", float(avg))] * n if avg is not None and n else []


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs: list[float]) -> float:
    """样本方差(n-1)。少于 2 个样本时没有离散度可谈,返回 0。"""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def _signal_to_noise(b: list[float], w: list[float]) -> float:
    """两组均值之差相对于噪音的倍数(近似 t 统计量)。

    ★ 为什么必须算这个:只比均值时,"$40 vs $38" 和 "$40 vs $5" 得到同一个结论
    ——"better 组更好"。但前者很可能只是抖动。
    **样本量回答"够不够多",信噪比回答"差异是不是真的"** —— 两个问题,缺一不可。

    分母为 0(两组内部都毫无波动)时返回一个大数:那种情况下差异是确定的。
    """
    if not b or not w:
        return 0.0
    se = (_var(b) / max(len(b), 1) + _var(w) / max(len(w), 1)) ** 0.5
    diff = _mean(b) - _mean(w)
    if se == 0:
        return 0.0 if diff == 0 else float("inf")
    return abs(diff) / se


def _split_by_time(per_tag: list[list[tuple[str, float]]]) -> tuple[list[float], list[float]]:
    """把一组标签的事件切成前半段 / 后半段。

    ★ 关键在于**每个标签各自切半再合并**,而不是把整组混在一起按日期切。
    混着切会把"标签构成变了"误判成"市场环境变了" —— 比如前半段几乎都是标签甲、
    后半段几乎都是标签乙,那两段的差异来自标签本身,与环境无关。
    各自切半能保证前后两段的标签构成一致,剩下的差异才更可能真的来自环境。
    """
    early: list[float] = []
    late: list[float] = []
    for events in per_tag:
        if len(events) < 2:
            continue
        ordered = sorted(events, key=lambda e: e[0])
        mid = len(ordered) // 2
        early += [v for _, v in ordered[:mid]]
        late += [v for _, v in ordered[mid:]]
    return early, late


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

    b_per_tag = [_tag_events(summary, tag) for tag in better]
    w_per_tag = [_tag_events(summary, tag) for tag in worse]
    b_events = [e for evs in b_per_tag for e in evs]
    w_events = [e for evs in w_per_tag for e in evs]

    if not b_events or not w_events:
        missing = [tag for tag in better + worse if not _tag_events(summary, tag)]
        return Verdict(rule, INCONCLUSIVE, INSUFFICIENT,
                       f"缺少归集数据的标签:{'/'.join(missing) or '(全部)'}",
                       "继续记录,或检查这些标签是否真的在用")

    b_vals = [v for _, v in b_events]
    w_vals = [v for _, v in w_events]
    b_avg, w_avg = _mean(b_vals), _mean(w_vals)
    n = min(len(b_vals), len(w_vals))
    diff = b_avg - w_avg
    snr = _signal_to_noise(b_vals, w_vals)

    detail = (f"{'/'.join(better)} 均 {'+' if b_avg >= 0 else '-'}${abs(b_avg):.2f}"
              f"({len(b_vals)} 事件)  vs  "
              f"{'/'.join(worse)} 均 {'+' if w_avg >= 0 else '-'}${abs(w_avg):.2f}"
              f"({len(w_vals)} 事件)")

    # ① 样本量:够不够多
    if n < weak_n:
        return Verdict(rule, INCONCLUSIVE, INSUFFICIENT,
                       f"{detail} —— 较小一侧仅 {n} 个事件(< {weak_n},方向不可信)",
                       f"攒够 {weak_n} 个决策事件才谈趋势,{min_n} 个才谈改状态", n)

    # ② 前后两段是否一致:规则错了,还是环境变了
    #
    # ★ 这一步必须排在信噪比之前。一次**完整的反转**(前半段 +12、后半段 -12)
    # 在全期均值里恰好互相抵消 —— 于是信噪比接近 0,判成"数据不足",
    # 而你永远看不到那个最重要的事实:这条规则的有效性掉过头。
    b_early, b_late = _split_by_time(b_per_tag)
    w_early, w_late = _split_by_time(w_per_tag)
    halves_usable = min(len(b_early), len(b_late), len(w_early), len(w_late)) >= max(3, weak_n // 2)
    if halves_usable:
        d_early = _mean(b_early) - _mean(w_early)
        d_late = _mean(b_late) - _mean(w_late)
        # 两段方向相反,且**各自都不是噪音** —— 随机翻个号不算环境变化
        if (d_early * d_late < 0
                and _signal_to_noise(b_early, w_early) >= 1.0
                and _signal_to_noise(b_late, w_late) >= 1.0):
            newer = "近期反转为不支持" if d_late < 0 else "近期反转为支持"
            return Verdict(
                rule, REGIME_SHIFT, WEAK,
                f"{detail} —— 前半段差 ${d_early:+.2f},后半段差 ${d_late:+.2f}({newer})",
                "**先别改状态。** 前后两段结论相反,更像是市场环境变了而不是规则错了 —— "
                "给这条规则补一个适用环境的条件(比如「仅在下跌趋势中」),然后分环境重新计数;"
                "确实要停用就降为 provisional 观察,不要直接 refuted。",
                n)

    # ③ 信噪比:差异是不是真的
    # 样本够了但差异淹在波动里 —— 这时候下结论,判的是运气不是规则。
    if snr < 1.0:
        return Verdict(
            rule, INCONCLUSIVE, INSUFFICIENT,
            f"{detail} —— 差值 ${abs(diff):.2f} 在噪音范围内(信噪比 {snr:.1f} < 1.0)",
            "样本够了,但两组差异小于组内波动 —— 继续记录,不要凭方向改状态", n)

    result = SUPPORTS if diff > 0 else REFUTES

    if n < min_n or snr < 2.0:
        why = f"较小一侧 {n} 个事件" if n < min_n else f"信噪比 {snr:.1f}(< 2.0)"
        return Verdict(rule, result, WEAK, f"{detail} —— {why}",
                       f"只是趋势提示,不足以改状态(需 ≥{min_n} 个决策事件且信噪比 ≥ 2.0)", n)

    conf = STRONG if n >= min_n * 2 and snr >= 3.0 else MODERATE
    if result == SUPPORTS:
        sug = ("证据足够,可**建议**升为 supported —— 仍需你本人批准"
               if rule.status == "hypothesis" else "")
    else:
        sug = ("数据与规则相反,且前后两段结论一致(不是环境变化)。"
               "修正判定标准,或降为 refuted(**保留记录,不要删**)—— "
               "从未被数据支持过的规则是包袱,不是资产")
    return Verdict(rule, result, conf,
                   f"{detail} —— 较小一侧 {n} 个事件,信噪比 {snr:.1f}", sug, n)


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


def stability_note(rule_id: str, status: str) -> str:
    """改状态前给出的稳定性提示(不阻断,但必须让人看见)。"""
    target = {"supported": SUPPORTS, "refuted": REFUTES, "retired": REFUTES}.get(status)
    if target is None:
        return ""
    ok, note = history_is_stable(rule_id, target)
    return ("✅ " if ok else "⚠️  ") + note


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
             test: str = "", path: Path | None = None,
             origin: str = "user", approved: bool = False) -> str:
    """新增一条假设。默认 hypothesis(→ observe),不能直接指导满额下单。

    `origin="ai"` 是"我不想自己定策略,由 AI 代拟"这条路径的落点:
    **必须 `approved=True`** —— 也就是你看过这条规则的原文、听过免责声明、
    明确说了可以。写进文件之后它和你自己写的规则**没有任何区别**:
    同样从 hypothesis 起步、同样按最低档尺寸、同样要靠台账数据才能升级、
    同样会被数据推翻。
    """
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
    if origin not in VALID_ORIGIN:
        raise ConfigError(f"origin 必须是 {'/'.join(VALID_ORIGIN)},实际 {origin!r}")
    if origin == "ai" and not approved:
        raise ConfigError(
            "AI 代拟的规则需要用户明确确认后才能写入。\n"
            "  先向用户完整念出规则原文与免责声明,得到确认后再加 --approved。\n"
            "  免责:AI 代拟的策略同样可能是错的,它没有你的风险承受能力信息,"
            "也不对结果负责。它和你自己写的规则一样,要靠你的台账数据来检验。"
        )
    origin_lines = ""
    if origin == "ai":
        origin_lines = (f'origin    = "ai"\n'
                        f'approved_at = {_toml_str(now_et().strftime("%Y-%m-%d %H:%M ET"))}\n')
    test_line = test or '{ type = "manual", how = "复盘时人工核查(待补:怎么查)" }'
    block = f"""

[[rule]]
id        = {_toml_str(rule_id)}
statement = {_toml_str(statement)}
kind      = {_toml_str(kind)}
test      = {test_line}
status    = "hypothesis"
{origin_lines}evidence  = []
last_audited = ""
"""
    _atomic_write(path, raw.rstrip("\n") + block)
    who = "AI 代拟、你已确认" if origin == "ai" else "你自己写的"
    return (f"已新增 {rule_id}({who};hypothesis → observe,尺寸按最低档)\n"
            f"  它从现在起走和其他规则完全一样的流程:靠台账数据检验,"
            f"升级要你批准,被推翻就降级。")


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
    ap.add_argument("--origin", default="user", choices=list(VALID_ORIGIN),
                    help="配合 --add-rule:user = 你自己写的;ai = AI 代拟(需 --approved)")
    args = ap.parse_args(argv)

    target = Path(args.file) if args.file else None
    try:
        if args.record_evidence:
            print("✅ " + record_evidence(*args.record_evidence, path=target))
            return 0
        if args.add_rule:
            print("✅ " + add_rule(args.add_rule[0], args.add_rule[1],
                                  kind=args.kind, path=target,
                                  origin=args.origin, approved=args.approved))
            return 0
        if args.set_status:
            rid, new_status = args.set_status
            note = stability_note(rid, new_status)
            if not args.approved:
                print("❌ 改状态会改变这条规则能否指导真钱下单 —— 需要用户明确批准。",
                      file=sys.stderr)
                if note:
                    print(f"   {note}", file=sys.stderr)
                print("   先向用户说明依据与建议,得到确认后再加 --approved 重跑。",
                      file=sys.stderr)
                return 1
            if note:
                print(note)          # 已批准也要把稳定性摆出来,留在会话记录里
            print("✅ " + set_status(rid, new_status, note=args.note, path=target))
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
