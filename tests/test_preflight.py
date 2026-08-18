"""preflight 闸门测试。

每一组用例都对应一次真实损失或一条硬纪律 —— 闸门必须真的拦得住,
否则它就只是换了个形式的散文。
"""

import copy
import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import preflight  # noqa: E402
from meigu_lib import Vocabulary  # noqa: E402
from preflight import ALLOW, DENY, DRY_RUN, run, validate_order  # noqa: E402

# 测试自带词表 —— 不依赖 config/reason-tags.toml(本机私有配置一改就会漂移)
TEST_VOCAB = Vocabulary(buy=("建仓", "加仓"), sell=("减仓", "清仓"))

ET = ZoneInfo("America/New_York")

# 2026-08-18 是周二、正常交易日
NOON = dt.datetime(2026, 8, 18, 13, 0, tzinfo=ET)

BASE_CFG = {
    "account": {"id": "000000000"},
    "execution": {
        "enabled": True,
        "dry_run": False,
        "require_confirmation": False,
        "max_order_usd": 80,
        "max_daily_usd": 200,
        "max_orders_per_day": 6,
        "intent_ttl_minutes": 15,
        "quote_max_age_minutes": 10,
        "kill_switch_file": "data/__nonexistent_halt__",
    },
    "trade": {"size_std": 50, "size_max": 80},
    "position": {"max_single_pct": 50, "reduce_pct_warn": 50, "residual_threshold_ratio": 0.5},
    "cash": {"floor_pct": 15},
}

BASE_ORDER = {
    "account_id": "000000000",
    "symbol": "AAAA",
    "side": "sell",
    "amount_usd": 30.0,
    "order_type": "market",
    "market_hours": "regular_hours",
    "intent": "partial",
    "reason_tag": "减仓",
    "ref_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    "analysis_at_et": "2026-08-18 12:55",
    "quote_timestamp_et": "2026-08-18 12:58",
    "position": {"market_value": 120.0, "qty": 0.3, "avg_cost": 390.0},
    "portfolio": {"total_value": 500.0, "buying_power": 90.0,
                  "cash": 90.0, "equity_value": 410.0},
    "today_orders": [],
}


def order(**overrides) -> dict:
    o = copy.deepcopy(BASE_ORDER)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(o.get(k), dict):
            o[k] = {**o[k], **v}
        else:
            o[k] = v
    return o


def cfg(**overrides) -> dict:
    c = copy.deepcopy(BASE_CFG)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k] = {**c[k], **v}
        else:
            c[k] = v
    return c


def names_failed(result) -> set[str]:
    return {c.name for c in result.checks if not c.ok}


class TestHappyPath(unittest.TestCase):
    def test_clean_order_is_allowed(self):
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, ALLOW, [c.detail for c in r.blockers])

    def test_dry_run_downgrades_verdict(self):
        r = run(order(), cfg(execution={"dry_run": True}), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DRY_RUN)
        self.assertEqual(r.blockers, [])


class TestAuthorization(unittest.TestCase):
    def test_disabled_execution_denies(self):
        """公开仓默认 enabled=false —— clone 者不继承任何人的授权。"""
        r = run(order(), cfg(execution={"enabled": False}), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("下单授权", names_failed(r))

    def test_kill_switch_denies(self):
        r = run(order(), cfg(execution={"kill_switch_file": "AGENTS.md"}), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("紧急停止开关", names_failed(r))

    def test_wrong_account_denies(self):
        r = run(order(account_id="111222333"), cfg(), NOON, vocab=TEST_VOCAB)  # privacy-allow
        self.assertEqual(r.verdict, DENY)
        self.assertIn("账户身份", names_failed(r))

    def test_missing_account_id_is_warning_only(self):
        o = order()
        del o["account_id"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, ALLOW)
        self.assertIn("账户身份", {c.name for c in r.warnings})


class TestMarketSession(unittest.TestCase):
    def test_regular_hours_ok(self):
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertNotIn("市场时段", names_failed(r))

    def test_non_trading_day_denies(self):
        # 2026-08-15 是周六
        r = run(order(), cfg(), dt.datetime(2026, 8, 15, 13, 0, tzinfo=ET))
        self.assertEqual(r.verdict, DENY)
        self.assertIn("市场时段", names_failed(r))

    def test_holiday_denies(self):
        # 2026-11-26 感恩节
        r = run(order(), cfg(), dt.datetime(2026, 11, 26, 13, 0, tzinfo=ET))
        self.assertIn("市场时段", names_failed(r))

    def test_after_close_market_order_denies(self):
        r = run(order(), cfg(), dt.datetime(2026, 8, 18, 16, 30, tzinfo=ET))
        self.assertIn("市场时段", names_failed(r))

    def test_extended_hours_limit_order_allowed(self):
        """分数股在延长时段可用限价单交易(按标的合格性) —— 不再当成绝对禁止。"""
        r = run(
            order(order_type="limit", market_hours="extended_hours", price=430.0),
            cfg(),
            dt.datetime(2026, 8, 18, 17, 0, tzinfo=ET),
        )
        self.assertNotIn("市场时段", names_failed(r))
        self.assertIn("延长时段分数股合格性", {c.name for c in r.warnings})

    def test_extended_hours_market_order_denies(self):
        r = run(
            order(order_type="market", market_hours="extended_hours"),
            cfg(),
            dt.datetime(2026, 8, 18, 17, 0, tzinfo=ET),
        )
        self.assertIn("市场时段", names_failed(r))

    def test_extended_hours_with_regular_session_flag_denies(self):
        r = run(
            order(order_type="limit", market_hours="regular_hours", price=430.0),
            cfg(),
            dt.datetime(2026, 8, 18, 17, 0, tzinfo=ET),
        )
        self.assertIn("市场时段", names_failed(r))

    def test_half_day_close_is_respected(self):
        """2026-11-27 是半日市,13:30 已收盘。"""
        r = run(order(), cfg(), dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET))
        self.assertIn("市场时段", names_failed(r))

    def test_deep_night_denies(self):
        r = run(order(), cfg(), dt.datetime(2026, 8, 18, 3, 0, tzinfo=ET))
        self.assertIn("市场时段", names_failed(r))


class TestFreshness(unittest.TestCase):
    def test_stale_intent_denies(self):
        """2026-07-09 权限弹窗冻结会话 6h —— 解冻后执行陈旧意图会更糟。"""
        r = run(order(analysis_at_et="2026-08-18 12:00"), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("意图时效", names_failed(r))

    def test_missing_intent_time_denies(self):
        o = order()
        del o["analysis_at_et"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("意图时效", names_failed(r))

    def test_future_intent_time_denies(self):
        r = run(order(analysis_at_et="2026-08-18 14:00"), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("意图时效", names_failed(r))

    def test_stale_quote_timestamp_denies(self):
        """2026-07-13:机器在分析与下单之间休眠 8.5h,靠券商时间戳才发现。"""
        r = run(order(quote_timestamp_et="2026-08-18 04:30"), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("报价时间戳熔断", names_failed(r))

    def test_missing_quote_timestamp_denies(self):
        o = order()
        del o["quote_timestamp_et"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("报价时间戳熔断", names_failed(r))


class TestSize(unittest.TestCase):
    def test_over_single_order_cap_denies(self):
        r = run(order(amount_usd=150.0, position={"market_value": 400.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("单笔金额上限", names_failed(r))

    def test_reduce_pct_guard_catches_the_2026_07_29_failure(self):
        """真实事故重演:仓位大跌到约等于一个标准尺寸,仍按标准尺寸减仓 → 卖出 91%。"""
        r = run(
            order(amount_usd=30.0, position={"market_value": 33.0}),
            cfg(),
            NOON,
        )
        self.assertEqual(r.verdict, DENY)
        self.assertIn("减仓占比", names_failed(r))
        detail = next(c.detail for c in r.checks if c.name == "减仓占比")
        self.assertIn("90.9%", detail)

    def test_explicit_close_intent_bypasses_pct_guard(self):
        r = run(
            order(amount_usd=33.0, position={"market_value": 33.0}, intent="close"),
            cfg(),
            NOON,
        )
        self.assertNotIn("减仓占比", names_failed(r))

    def test_residual_position_warns(self):
        # 卖 100 剩 20,低于残值线 50*0.5=25
        r = run(order(amount_usd=60.0, position={"market_value": 80.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("残值仓", {c.name for c in r.warnings})

    def test_zero_amount_denies(self):
        r = run(order(amount_usd=0.0), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("金额为正", names_failed(r))

    def test_amount_derived_from_qty_and_price(self):
        o = order()
        del o["amount_usd"]
        o["qty"], o["price"] = 0.1, 400.0
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        detail = next(c.detail for c in r.checks if c.name == "单笔金额上限")
        self.assertIn("40.00", detail)


class TestBuySideGates(unittest.TestCase):
    def _buy(self, **kw):
        return order(side="buy", reason_tag="加仓", intent="", **kw)

    def test_insufficient_buying_power_denies(self):
        r = run(self._buy(amount_usd=50.0, portfolio={"buying_power": 20.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("买力充足", names_failed(r))

    def test_concentration_cap_denies(self):
        # 该标的已占 200/410,再买 60 → (200+60)/(410+60) ≈ 55% > 50%
        r = run(
            self._buy(amount_usd=60.0, position={"market_value": 200.0}),
            cfg(),
            NOON,
        )
        self.assertIn("集中度", names_failed(r))

    def test_cash_floor_denies(self):
        # BP 90,买 60 → 剩 30/500 = 6% < 15%
        r = run(self._buy(amount_usd=60.0), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("现金底线", names_failed(r))

    def test_buy_does_not_run_sell_checks(self):
        r = run(self._buy(amount_usd=20.0, portfolio={"buying_power": 200.0,
                                                      "total_value": 2000.0}), cfg(), NOON)
        self.assertNotIn("减仓占比", {c.name for c in r.checks})


class TestReasonTag(unittest.TestCase):
    def test_sell_tag_on_buy_denies(self):
        r = run(order(side="buy", reason_tag="减仓"), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("理由标签", names_failed(r))

    def test_unknown_tag_denies(self):
        r = run(order(reason_tag="随便卖卖"), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("理由标签", names_failed(r))


class TestDailyLimits(unittest.TestCase):
    def test_daily_amount_cap_denies(self):
        today = [{"symbol": "CCCC", "side": "sell", "amount": 180.0}]
        r = run(order(amount_usd=30.0, today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("单日累计(sell)", names_failed(r))

    def test_daily_count_cap_denies(self):
        today = [{"symbol": f"S{i}", "side": "buy", "amount": 1.0} for i in range(6)]
        r = run(order(today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("单日笔数", names_failed(r))

    def test_same_symbol_twice_denies(self):
        """同一标的当日不重复操作(preflight 硬闸门)。"""
        today = [{"symbol": "aaaa", "side": "buy", "amount": 30.0}]
        r = run(order(today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("同标的当日重复", names_failed(r))

    def test_different_symbol_same_checkpoint_is_fine(self):
        """v4.12:同一检查点可对不同标的执行多笔独立操作。"""
        today = [{"symbol": "CCCC", "side": "buy", "amount": 30.0}]
        r = run(order(today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertNotIn("同标的当日重复", names_failed(r))
        self.assertEqual(r.verdict, ALLOW)


class TestRefId(unittest.TestCase):
    def test_missing_ref_id_denies(self):
        o = order()
        del o["ref_id"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("ref_id", names_failed(r))

    def test_malformed_ref_id_denies(self):
        r = run(order(ref_id="not-a-uuid"), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("ref_id", names_failed(r))

    def test_reused_ref_id_denies(self):
        today = [{"symbol": "CCCC", "side": "buy", "amount": 1.0,
                  "ref_id": BASE_ORDER["ref_id"]}]
        r = run(order(today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("ref_id 未重复", names_failed(r))

    def test_dedup_coverage_limitation_is_disclosed(self):
        """去重覆盖面有限这件事必须显式说出来,不能默认让人以为是完备的。"""
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        check = next(c for c in r.checks if c.name == "ref_id 未重复")
        self.assertTrue(check.ok)
        self.assertIn("order_id", check.hint)
        self.assertIn("不是完备保证", check.hint)


class TestInputValidation(unittest.TestCase):
    def test_missing_required_fields(self):
        errs = validate_order({"side": "buy", "amount_usd": 10})
        self.assertTrue(any("symbol" in e for e in errs))
        self.assertTrue(any("reason_tag" in e for e in errs))

    def test_bad_side(self):
        errs = validate_order({"symbol": "X", "side": "hold",
                               "reason_tag": "建仓", "amount_usd": 1})
        self.assertTrue(any("side" in e for e in errs))

    def test_no_amount_or_qty(self):
        errs = validate_order({"symbol": "X", "side": "buy", "reason_tag": "建仓"})
        self.assertTrue(any("amount_usd 或 qty" in e for e in errs))

    def test_clean_order_validates(self):
        self.assertEqual(validate_order(BASE_ORDER), [])

    def test_example_order_is_valid(self):
        self.assertEqual(validate_order(preflight.EXAMPLE_ORDER), [])


class TestCli(unittest.TestCase):
    """CLI 入口测试 —— run() 通过不代表命令行可用。"""

    def _main(self, argv):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = preflight.main(argv)
        return code, buf.getvalue()

    def test_example_flag_works_alone(self):
        """--example 不需要输入源 —— 曾因放进 required 互斥组而无法单独使用。"""
        code, out = self._main(["--example"])
        self.assertEqual(code, 0)
        self.assertIn("reason_tag", out)
        import json as _json

        self.assertEqual(validate_order(_json.loads(out)), [])

    def test_missing_input_source_errors(self):
        with self.assertRaises(SystemExit):
            preflight.main([])

    def test_order_file_json_output(self):
        import json as _json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            _json.dump(BASE_ORDER, fh)
            path = fh.name
        code, out = self._main(["--order-file", path, "--json",
                                "--now-et", "2026-08-18 13:00"])
        payload = _json.loads(out)
        self.assertIn(payload["verdict"], (ALLOW, DRY_RUN, DENY))
        self.assertIn("checks", payload)

    def test_deny_exits_nonzero(self):
        import json as _json
        import tempfile

        bad = dict(BASE_ORDER, analysis_at_et="2026-08-18 08:00")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            _json.dump(bad, fh)
            path = fh.name
        code, _ = self._main(["--order-file", path, "--now-et", "2026-08-18 13:00"])
        self.assertEqual(code, 1)

    def test_malformed_json_exits_two(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("{not json")
            path = fh.name
        code = preflight.main(["--order-file", path])
        self.assertEqual(code, 2)


class TestMultipleBlockers(unittest.TestCase):
    def test_all_blockers_reported_not_just_first(self):
        """一次跑完所有闸门,不要 fail-fast —— 用户需要一次看到全部问题。"""
        r = run(
            order(
                amount_usd=500.0,
                analysis_at_et="2026-08-18 08:00",
                quote_timestamp_et="2026-08-18 08:00",
                reason_tag="瞎卖",
                ref_id="bad",
            ),
            cfg(execution={"enabled": False}),
            NOON,
        )
        self.assertEqual(r.verdict, DENY)
        self.assertGreaterEqual(len(r.blockers), 5)


if __name__ == "__main__":
    unittest.main()


class TestRuleScope(unittest.TestCase):
    """★ 未经数据支持的假设不能单独授权真钱下单。"""

    def test_missing_rule_ids_is_warning_only(self):
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("规则作用域", {c.name for c in r.warnings})
        self.assertEqual(r.verdict, ALLOW)

    def test_unknown_rule_id_denies(self):
        r = run(order(rule_ids=["根本不存在的规则"]), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("规则作用域", names_failed(r))
