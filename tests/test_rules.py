"""规则加载与审计测试。

`config/rules.toml` 是这个项目"自我进化"机制的核心数据结构:
用户声明**自己的**可检验命题,系统用台账数据判定它成立与否。

本仓库**不预设任何市场判断类规则** —— 所以这些测试用的规则全部是测试固件,
不代表任何推荐策略。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rules as R  # noqa: E402
from meigu_lib import TRADE_COLUMNS, ConfigError, Vocabulary, parse_trades  # noqa: E402
from rules import (  # noqa: E402
    DEFAULT_MIN_SAMPLES,
    DEFAULT_WEAK_MIN_SAMPLES,
    ENFORCED,
    INCONCLUSIVE,
    MANUAL,
    MODERATE,
    REFUTES,
    SUPPORTS,
    WEAK,
    Rule,
    audit_rule,
    audit_rules,
    load_rules,
)
from stats import summarize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VOCAB = Vocabulary(buy=("建仓", "加仓"), sell=("甲", "乙", "丙"))
HEADER = "\t".join(TRADE_COLUMNS)


def tsv(*rows: str) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8")
    fh.write(HEADER + "\n")
    for r in rows:
        fh.write(r + "\n")
    fh.close()
    return Path(fh.name)


def row(date, sym, side, qty, price, amount, tag):
    return "\t".join([date, "10:33", sym, side, str(qty), str(price), str(amount), tag, "", ""])


def _day(i: int) -> tuple[str, str]:
    """把序号映射成一对合法日期(买入月 / 卖出月),避免超出月份天数。"""
    m, d = divmod(i - 1, 28)
    return (f"2026-{1 + m:02d}-{d + 1:02d}", f"2026-{7 + m:02d}-{d + 1:02d}")


def summary_of(*rows: str) -> dict:
    return summarize(parse_trades(tsv(*rows), vocab=VOCAB), vocab=VOCAB)


def toml_file(text: str) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    return Path(fh.name)


# ------------------------------------------------------------------ 加载与校验
class TestLoad(unittest.TestCase):
    def test_loads_valid_rules(self):
        p = toml_file('''
[[rule]]
id = "a"
statement = "一句能被证伪的话"
kind = "process"
test = { type = "manual", how = "复盘时人工过" }
status = "invariant"
''')
        rules, src, is_example = load_rules(path=p)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].id, "a")
        self.assertTrue(rules[0].active)

    def test_missing_file_returns_empty(self):
        rules, src, _ = load_rules(path=Path("/nonexistent/rules.toml"))
        self.assertEqual(rules, [])

    def test_rejects_duplicate_id(self):
        p = toml_file('''
[[rule]]
id = "dup"
statement = "x"
test = { type = "manual", how = "h" }
[[rule]]
id = "dup"
statement = "y"
test = { type = "manual", how = "h" }
''')
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path=p)
        self.assertIn("id 重复", str(ctx.exception))

    def test_rejects_empty_statement(self):
        p = toml_file('[[rule]]\nid = "a"\nstatement = "  "\ntest = { type = "manual", how = "h" }\n')
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path=p)
        self.assertIn("statement 为空", str(ctx.exception))

    def test_rejects_bad_status(self):
        p = toml_file('[[rule]]\nid="a"\nstatement="x"\nstatus="maybe"\ntest={type="manual",how="h"}\n')
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path=p)
        self.assertIn("status", str(ctx.exception))

    def test_rejects_bad_test_type(self):
        p = toml_file('[[rule]]\nid="a"\nstatement="x"\ntest={type="vibes"}\n')
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path=p)
        self.assertIn("test.type", str(ctx.exception))

    def test_tag_compare_requires_both_sides(self):
        p = toml_file('[[rule]]\nid="a"\nstatement="x"\ntest={type="tag_compare",better=["甲"]}\n')
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path=p)
        self.assertIn("better 与 worse", str(ctx.exception))

    def test_shipped_example_is_valid(self):
        """config/rules.example.toml 必须自身合法,否则新用户第一步就卡住。"""
        rules, src, is_example = load_rules(path=ROOT / "config" / "rules.example.toml")
        self.assertTrue(rules)
        self.assertTrue(all(r.kind == "process" for r in rules),
                        "样例里不得含 market 类规则 —— 市场判断必须由用户自己写")

    def test_demo_fixture_is_valid(self):
        from meigu_lib import load_vocabulary

        demo_vocab = load_vocabulary(path=ROOT / "examples" / "sample-reason-tags.toml")
        rules, _, _ = load_rules(path=ROOT / "examples" / "sample-rules.toml",
                                 vocab=demo_vocab)
        self.assertTrue(any(r.kind == "market" for r in rules))

    def test_demo_market_rule_is_observe_only(self):
        """演示里的假设也必须是 observe —— 示范正确的默认姿态。"""
        from meigu_lib import load_vocabulary

        demo_vocab = load_vocabulary(path=ROOT / "examples" / "sample-reason-tags.toml")
        rules, _, _ = load_rules(path=ROOT / "examples" / "sample-rules.toml",
                                 vocab=demo_vocab)
        for r in rules:
            if r.kind == "market":
                self.assertFalse(r.may_authorize_live, r.id)


class TestRuleState(unittest.TestCase):
    def test_active_states(self):
        for status in ("enforced", "supported", "hypothesis"):
            self.assertTrue(Rule("a", "x", status=status).active, status)

    def test_inactive_states(self):
        for status in ("refuted", "retired"):
            self.assertFalse(Rule("a", "x", status=status).active, status)

    def test_audit_rules_skips_inactive_by_default(self):
        rs = [Rule("a", "x", test={"type": "manual", "how": "h"}, status="retired"),
              Rule("b", "y", test={"type": "manual", "how": "h"}, status="hypothesis")]
        self.assertEqual(len(audit_rules(rs, {})), 1)
        self.assertEqual(len(audit_rules(rs, {}, include_inactive=True)), 2)


# ---------------------------------------------------------------------- 审计
class TestAuditNonComparative(unittest.TestCase):
    def test_enforced_by(self):
        v = audit_rule(Rule("a", "x", test={"type": "enforced_by", "by": "preflight:某闸门"}), {})
        self.assertEqual(v.result, ENFORCED)
        self.assertIn("某闸门", v.detail)

    def test_manual(self):
        v = audit_rule(Rule("a", "x", test={"type": "manual", "how": "怎么查"}), {})
        self.assertEqual(v.result, MANUAL)
        self.assertIn("怎么查", v.detail)

    def test_unknown_type(self):
        v = audit_rule(Rule("a", "x", test={"type": "???"}), {})
        self.assertEqual(v.result, INCONCLUSIVE)


class TestAuditTagCompare(unittest.TestCase):
    def _rule(self, better, worse):
        return Rule("r", "测试命题", kind="market",
                    test={"type": "tag_compare", "better": better, "worse": worse})

    def _ledger(self, better_pnls, worse_pnls):
        """构造若干平仓配对,让 better/worse 标签各自有指定的实现盈亏。"""
        rows, day = [], 1
        for pnl in better_pnls:
            d = _day(day)
            rows.append(row(d[0], f"B{day}", "buy", 1, 100.0, 100.0, "建仓"))
            rows.append(row(d[1], f"B{day}", "sell", 1, 100.0 + pnl, 100.0 + pnl, "甲"))
            day += 1
        for pnl in worse_pnls:
            d = _day(day)
            rows.append(row(d[0], f"W{day}", "buy", 1, 100.0, 100.0, "建仓"))
            rows.append(row(d[1], f"W{day}", "sell", 1, 100.0 + pnl, 100.0 + pnl, "乙"))
            day += 1
        return summary_of(*rows)

    def test_supported_when_better_group_wins(self):
        s = self._ledger([10] * 20, [-5] * 20)
        v = audit_rule(self._rule(["甲"], ["乙"]), s)
        self.assertEqual(v.result, SUPPORTS)
        self.assertIn("甲", v.detail)

    def test_refuted_when_better_group_loses(self):
        s = self._ledger([-5] * 20, [10] * 20)
        v = audit_rule(self._rule(["甲"], ["乙"]), s)
        self.assertEqual(v.result, REFUTES)
        self.assertIn("包袱", v.suggestion)

    def test_insufficient_when_sample_too_small(self):
        """样本太少不下结论 —— 宁可说数据不足,也不给假装精确的判定。"""
        s = self._ledger([10] * 3, [-5] * 3)
        v = audit_rule(self._rule(["甲"], ["乙"]), s)
        self.assertEqual(v.result, INCONCLUSIVE)
        self.assertIn("方向不可信", v.detail)

    def test_insufficient_when_tag_absent(self):
        s = self._ledger([10] * 20, [])
        v = audit_rule(self._rule(["甲"], ["乙"]), s)
        self.assertEqual(v.result, INCONCLUSIVE)
        self.assertIn("乙", v.detail)

    def test_worse_group_is_weighted_not_cherry_picked(self):
        """worse 组按事件数加权合并 —— 不能靠只列一个软柿子来制造"支持"。"""
        rows, day = [], 1
        seq = [(5, "甲")] * 20 + [(-9, "乙")] * 20 + [(9, "丙")] * 20
        for pnl, tag in seq:
            d = _day(day)
            rows.append(row(d[0], f"S{day}", "buy", 1, 100.0, 100.0, "建仓"))
            rows.append(row(d[1], f"S{day}", "sell", 1, 100.0 + pnl, 100.0 + pnl, tag))
            day += 1
        s = summary_of(*rows)
        v = audit_rule(self._rule(["甲"], ["乙", "丙"]), s)
        # 乙(-9)与丙(+9)等量加权后均值 0,甲(+5)高于它 → 支持
        self.assertEqual(v.result, SUPPORTS)
        # 但只挑软柿子(乙)比较会得到同样结论 —— 差别在于列了丙之后仍然成立
        self.assertEqual(audit_rule(self._rule(["甲"], ["丙"]), s).result, REFUTES)

    def test_suggests_promotion_for_hypothesis(self):
        s = self._ledger([10] * 20, [-5] * 20)
        r = self._rule(["甲"], ["乙"])
        r.status = "hypothesis"
        self.assertIn("supported", audit_rule(r, s).suggestion)


class TestNoBuiltInStrategy(unittest.TestCase):
    """守卫:本仓库不得内置任何市场判断类规则。"""

    def test_example_has_no_market_rules(self):
        rules, _, _ = load_rules(path=ROOT / "config" / "rules.example.toml")
        market = [r for r in rules if r.kind == "market"]
        self.assertEqual(
            market, [],
            "config/rules.example.toml 里出现了 market 类规则:"
            f"{[r.id for r in market]}。市场判断必须由用户自己写,仓库只给问题不给答案。",
        )

    def test_example_process_rules_are_all_enforced_or_manual(self):
        """流程纪律应当有程序或明确的人工核查方式兜底,不能只是一句口号。"""
        rules, _, _ = load_rules(path=ROOT / "config" / "rules.example.toml")
        for r in rules:
            self.assertIn(r.test_type, ("enforced_by", "manual"), r.id)


if __name__ == "__main__":
    unittest.main()


class TestWriteCommands(unittest.TestCase):
    """自我进化闭环的写回环节 —— 必须由脚本做,agent 手改 TOML 会静默破坏审计。"""

    def setUp(self):
        import shutil

        self.path = toml_file('''
[[rule]]
id        = "w1"
statement = "一句能被证伪的话"
kind      = "market"
test      = { type = "manual", how = "人工核查" }
status    = "hypothesis"
evidence  = []
last_audited = ""

[[rule]]
id        = "w2"
statement = "第二条"
kind      = "market"
test      = { type = "manual", how = "人工核查" }
status    = "hypothesis"
evidence  = ["已有一条"]
last_audited = ""
''')
        self.shutil = shutil

    def _rule(self, rid):
        rules, _, _ = load_rules(path=self.path)
        return next(r for r in rules if r.id == rid)

    def test_record_evidence_into_empty_list(self):
        R.record_evidence("w1", "2026-08-19 第一条证据", path=self.path)
        self.assertEqual(self._rule("w1").evidence, ["2026-08-19 第一条证据"])

    def test_record_evidence_appends_to_existing(self):
        R.record_evidence("w2", "新的一条", path=self.path)
        self.assertEqual(self._rule("w2").evidence, ["已有一条", "新的一条"])

    def test_record_evidence_does_not_touch_other_rules(self):
        R.record_evidence("w1", "只给 w1", path=self.path)
        self.assertEqual(self._rule("w2").evidence, ["已有一条"])

    def test_record_evidence_escapes_quotes(self):
        R.record_evidence("w1", '含"引号"的证据', path=self.path)
        self.assertIn('含"引号"的证据', self._rule("w1").evidence)

    def test_unknown_id_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            R.record_evidence("nope", "x", path=self.path)
        self.assertIn("找不到", str(ctx.exception))

    def test_set_status_promotes(self):
        R.set_status("w1", "supported", path=self.path)
        r = self._rule("w1")
        self.assertEqual(r.status, "supported")
        self.assertTrue(r.may_authorize_live)

    def test_set_status_updates_last_audited(self):
        R.set_status("w1", "supported", path=self.path)
        self.assertNotEqual(self._rule("w1").last_audited, "")

    def test_demotion_also_disables_scope(self):
        """降级必须同步停用 —— 否则会出现『状态说停用、作用域还在跑』。"""
        R.set_status("w1", "refuted", path=self.path)
        r = self._rule("w1")
        self.assertEqual(r.status, "refuted")
        self.assertEqual(r.scope, "none")
        self.assertFalse(r.active)

    def test_set_status_note_becomes_evidence(self):
        R.set_status("w1", "supported", note="数据支持", path=self.path)
        self.assertTrue(any("数据支持" in e for e in self._rule("w1").evidence))

    def test_set_status_rejects_unknown_status(self):
        with self.assertRaises(ConfigError):
            R.set_status("w1", "maybe", path=self.path)

    def test_add_rule_defaults_to_observe(self):
        """新假设默认只能观察 —— 不该一写出来就能指导满额下单。"""
        R.add_rule("w3", "新的可证伪命题", path=self.path)
        r = self._rule("w3")
        self.assertEqual(r.status, "hypothesis")
        self.assertEqual(r.scope, "observe")
        self.assertFalse(r.may_authorize_live)

    def test_add_rule_rejects_duplicate_id(self):
        with self.assertRaises(ConfigError) as ctx:
            R.add_rule("w1", "重复 id", path=self.path)
        self.assertIn("已存在", str(ctx.exception))

    def test_file_stays_parseable_after_every_write(self):
        """每次写入后文件都必须仍然合法 —— 这是整套审计不失真的前提。"""
        R.record_evidence("w1", "a", path=self.path)
        R.set_status("w1", "supported", note="b", path=self.path)
        R.add_rule("w4", "又一条", path=self.path)
        R.set_status("w4", "retired", path=self.path)
        rules, _, _ = load_rules(path=self.path)
        self.assertEqual([r.id for r in rules], ["w1", "w2", "w4"])
        self.assertEqual(next(r for r in rules if r.id == "w4").status, "retired")


class TestSetStatusRequiresApproval(unittest.TestCase):
    def test_cli_refuses_without_approved_flag(self):
        """改状态会改变这条规则能否指导真钱下单 —— CLI 必须拦住未批准的调用。"""
        p = toml_file('''
[[rule]]
id        = "x1"
statement = "x"
kind      = "market"
test      = { type = "manual", how = "h" }
status    = "hypothesis"
evidence  = []
last_audited = ""
''')
        code = R.main(["--file", str(p), "--set-status", "x1", "supported"])
        self.assertEqual(code, 1)
        rules, _, _ = load_rules(path=p)
        self.assertEqual(rules[0].status, "hypothesis", "未批准却被改了")

    def test_cli_allows_with_approved_flag(self):
        p = toml_file('''
[[rule]]
id        = "x2"
statement = "x"
kind      = "market"
test      = { type = "manual", how = "h" }
status    = "hypothesis"
evidence  = []
last_audited = ""
''')
        code = R.main(["--file", str(p), "--set-status", "x2", "supported", "--approved"])
        self.assertEqual(code, 0)
        rules, _, _ = load_rules(path=p)
        self.assertEqual(rules[0].status, "supported")

    def test_cli_record_evidence_needs_no_approval(self):
        p = toml_file('''
[[rule]]
id        = "x3"
statement = "x"
kind      = "market"
test      = { type = "manual", how = "h" }
status    = "hypothesis"
evidence  = []
last_audited = ""
''')
        self.assertEqual(R.main(["--file", str(p), "--record-evidence", "x3", "事实一条"]), 0)
        rules, _, _ = load_rules(path=p)
        self.assertEqual(rules[0].evidence, ["事实一条"])


class TestWriteRobustness(unittest.TestCase):
    """写入必须原子且经校验 —— 一次写坏就会让整套审计静默失真。"""

    def setUp(self):
        self.path = toml_file('''
[[rule]]
id        = "r1"
statement = "一句能被证伪的话"
kind      = "market"
test      = { type = "manual", how = "人工核查" }
status    = "hypothesis"
evidence  = []
last_audited = ""
''')
        self.before = self.path.read_text(encoding="utf-8")

    def test_evidence_with_newlines_does_not_corrupt(self):
        """含换行的证据必须被转义 —— 直接写进去 TOML 立刻损坏。"""
        R.record_evidence("r1", "第一行\n第二行\t带制表符", path=self.path)
        rules, _, _ = load_rules(path=self.path)   # 仍可解析即通过
        self.assertIn("第一行", rules[0].evidence[0])
        self.assertIn("第二行", rules[0].evidence[0])
        # 文件里必须是转义形式,不能是裸换行(裸换行会让 TOML 立刻损坏)
        self.assertNotIn("第一行\n第二行", self.path.read_text(encoding="utf-8"))

    def test_evidence_with_quotes_does_not_corrupt(self):
        R.record_evidence("r1", '他说"这条不成立"', path=self.path)
        rules, _, _ = load_rules(path=self.path)
        self.assertIn("这条不成立", rules[0].evidence[0])

    def test_invalid_kind_is_rejected_before_writing(self):
        """非法 kind 必须当场拒绝,不能"报成功然后文件读不出来"。"""
        with self.assertRaises(ConfigError):
            R.add_rule("r2", "x", kind="nonsense", path=self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.before)
        load_rules(path=self.path)                 # 文件仍然合法

    def test_reenable_clears_stale_none_scope(self):
        """refuted → supported 后必须能重新启用,否则规则永久卡死。"""
        R.set_status("r1", "refuted", path=self.path)
        self.assertEqual(load_rules(path=self.path)[0][0].scope, "none")
        R.set_status("r1", "supported", path=self.path)
        r = load_rules(path=self.path)[0][0]
        self.assertEqual(r.status, "supported")
        self.assertEqual(r.scope, "live")
        self.assertTrue(r.may_authorize_live)

    def test_failed_write_leaves_original_untouched(self):
        """写入会产生非法状态时必须回滚,原文件一个字节都不能变。"""
        with self.assertRaises(ConfigError):
            R.set_status("r1", "enforced", path=self.path)   # market + 非 enforced_by
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.before)

    def test_provisional_is_a_middle_tier(self):
        R.set_status("r1", "provisional", path=self.path)
        r = load_rules(path=self.path)[0][0]
        self.assertEqual(r.scope, "live")
        self.assertTrue(r.may_authorize_live)
