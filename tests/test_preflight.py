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


class _OKStep:
    """接入状态机的「全部就绪」桩。"""

    def __init__(self, state: str):
        self.state, self.ok, self.detail, self.todo = state, True, "ok", []


def all_steps_ok():
    from setup import ORDER

    return [_OKStep(s) for s in ORDER], ORDER[-1]


_REAL_SETUP_EVAL = preflight._setup_evaluate


def setUpModule():
    """★ 闸门测试不得依赖本机的真实接入状态。

    「接入状态」闸门会去读 data/setup-state.json;若不打桩,本机做没做过
    只读验证就会决定测试是绿是红 —— 那是我们已经修过一次的病(测试依赖环境配置)。
    需要验证该闸门本身的用例,自己在方法内替换这个桩。
    """
    preflight._setup_evaluate = all_steps_ok


def tearDownModule():
    preflight._setup_evaluate = _REAL_SETUP_EVAL


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

    def test_missing_account_id_is_denied(self):
        """「无法核实」不等于「核实通过」—— 下错子账户不可撤销。"""
        o = order()
        del o["account_id"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("账户身份", names_failed(r))


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
    def test_buy_over_single_order_cap_denies(self):
        r = run(order(side="buy", reason_tag="建仓", amount_usd=150.0,
                      portfolio={"buying_power": 2000.0, "total_value": 5000.0},
                      position={"market_value": 400.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("单笔金额上限", names_failed(r))

    def test_sell_is_not_capped_by_max_order_usd(self):
        """★ 退出敞口不受单笔上限约束 —— 否则会变成"能建仓、不能完整平仓"。"""
        r = run(order(amount_usd=120.0, intent="close",
                      position={"market_value": 120.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertNotIn("退出尺寸上限", names_failed(r))
        self.assertNotIn("单笔金额上限", {c.name for c in r.checks})
        self.assertEqual(r.verdict, ALLOW, [c.detail for c in r.blockers])

    def test_sell_beyond_position_value_denies(self):
        r = run(order(amount_usd=500.0, intent="close",
                      position={"market_value": 120.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("退出尺寸上限", names_failed(r))

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
        # 金额 <= 0 现在在结构校验层就被挡下,不必等到金额闸门
        self.assertIn("订单结构", names_failed(r))

    def test_amount_derived_from_qty_and_price(self):
        o = order()
        del o["amount_usd"]
        o["qty"], o["price"] = 0.1, 400.0
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        detail = next(c.detail for c in r.checks if c.name == "退出尺寸上限")
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
    def test_daily_amount_cap_denies_buys(self):
        today = [{"symbol": "CCCC", "side": "buy", "amount": 180.0}]
        r = run(order(side="buy", reason_tag="建仓", amount_usd=30.0, today_orders=today,
                      portfolio={"buying_power": 2000.0, "total_value": 5000.0},
                      position={"market_value": 50.0}), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("单日累计(buy)", names_failed(r))

    def test_daily_amount_cap_exempts_sells(self):
        """退出敞口不该被单日金额上限挡住。"""
        today = [{"symbol": "CCCC", "side": "sell", "amount": 180.0}]
        r = run(order(amount_usd=30.0, today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertNotIn("单日累计(sell)", names_failed(r))

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




class TestEvidenceSizing(unittest.TestCase):
    """★ 证据强度只约束新增风险(买入),且只有 primary_rule_id 决定尺寸。"""

    def _buy(self, **kw):
        return order(side="buy", reason_tag="建仓", intent="",
                     portfolio={"buying_power": 2000.0, "total_value": 5000.0},
                     position={"market_value": 50.0}, **kw)

    def test_sell_is_exempt_from_evidence_sizing(self):
        r = run(order(amount_usd=100.0, position={"market_value": 200.0}),
                cfg(), NOON, vocab=TEST_VOCAB)
        c = next(x for x in r.checks if x.name.startswith("证据尺寸"))
        self.assertTrue(c.ok)
        self.assertIn("卖出降低风险", c.detail)

    def test_buy_without_primary_rule_uses_lowest_tier(self):
        c = cfg(execution={"max_order_usd": 80, "size_scale_observe": 0.4})
        self.assertEqual(run(self._buy(amount_usd=30.0), c, NOON, vocab=TEST_VOCAB).verdict, ALLOW)
        r = run(self._buy(amount_usd=40.0), c, NOON, vocab=TEST_VOCAB)
        self.assertIn("证据尺寸", names_failed(r))
        self.assertIn("32.00", next(x.detail for x in r.checks if x.name == "证据尺寸"))

    def test_hint_states_the_allowed_amount(self):
        """超限时必须直接给出允许金额 —— 自动化不能要求人工换算。"""
        r = run(self._buy(amount_usd=40.0),
                cfg(execution={"max_order_usd": 80, "size_scale_observe": 0.4}),
                NOON, vocab=TEST_VOCAB)
        self.assertIn("$32.00", next(x.hint for x in r.checks if x.name == "证据尺寸"))

    def test_unknown_primary_rule_denies(self):
        r = run(self._buy(amount_usd=10.0, primary_rule_id="不存在的规则"),
                cfg(), NOON, vocab=TEST_VOCAB)
        self.assertIn("证据尺寸", names_failed(r))


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
        code, out = self._main(["--order-file", path, "--json", "--demo",
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
        code, _ = self._main(["--order-file", path, "--demo",
                              "--now-et", "2026-08-18 13:00"])
        self.assertEqual(code, 1)

    def test_time_and_tag_overrides_require_demo(self):
        """★ 这两个参数会改变风控判定的输入 —— 真钱路径上不该存在这种开关。

        评审实测:临时 profile 把单笔上限改成 $100,000,一笔本该 DENY 的
        $500 买单就变成了 ALLOW。**能被调用方替换的上限不是上限。**
        """
        import json as _json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            _json.dump(BASE_ORDER, fh)
            path = fh.name
        for extra in (["--now-et", "2026-08-18 13:00"],
                      ["--tags", "examples/sample-reason-tags.toml"]):
            with self.subTest(flag=extra[0]):
                code, _ = self._main(["--order-file", path] + extra)
                self.assertEqual(code, 2, f"{extra[0]} 不带 --demo 应当被拒绝")

    def test_profile_flag_no_longer_exists(self):
        """`--profile <任意路径>` 等于给下单路径开一个"自带风控配置"的入口。"""
        self.assertNotIn("--profile", preflight.main.__doc__ or "")
        import argparse
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit):
            preflight.main(["--order-file", "x.json", "--profile", "/tmp/evil.toml"])
        self.assertIn("unrecognized arguments", buf.getvalue())

    def test_demo_never_returns_allow(self):
        """演示不该输出"可以下单" —— 那是会被截图传播的一句话。"""
        import json as _json
        import tempfile

        clean = dict(BASE_ORDER)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            _json.dump(clean, fh)
            path = fh.name
        code, out = self._main(["--order-file", path, "--json", "--demo",
                                "--tags", "examples/sample-reason-tags.toml",
                                "--now-et", "2026-08-18 13:00"])
        self.assertNotEqual(_json.loads(out)["verdict"], ALLOW)

    def test_demo_profile_is_locked_down(self):
        """演示配置必须 dry_run + 占位账户,否则 --demo 直接拒绝启动。"""
        cfg = preflight._demo_config()
        self.assertIs(cfg["execution"]["dry_run"], True)
        self.assertEqual(cfg["account"]["id"], "000000000")

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


class TestSetupGate(unittest.TestCase):
    """★ 状态机必须真的接在下单路径上,而不是只做提示。

    对抗测试原文:构造 setup 未完成、订单本身合法的场景,preflight 仍返回 ALLOW。
    """

    def _incomplete(self):
        steps, _ = all_steps_ok()
        steps[1].ok, steps[1].detail = False, "尚未完成券商 MCP 只读验证"
        steps[4].ok, steps[4].detail = False, "尚未完成 dry-run 演练"
        return steps, "UNINITIALIZED"

    def test_live_order_denied_when_setup_incomplete(self):
        preflight._setup_evaluate = self._incomplete
        self.addCleanup(lambda: setattr(preflight, "_setup_evaluate", all_steps_ok))
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("接入状态", names_failed(r))

    def test_dry_run_is_not_blocked_by_setup(self):
        """演练本身就是第 5 步 —— 要求它先完成第 5 步是死循环。"""
        preflight._setup_evaluate = self._incomplete
        self.addCleanup(lambda: setattr(preflight, "_setup_evaluate", all_steps_ok))
        c = cfg()
        c["execution"]["dry_run"] = True
        r = run(order(), c, NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DRY_RUN)

    def test_unreadable_setup_state_fails_closed(self):
        def boom():
            raise RuntimeError("state unreadable")

        preflight._setup_evaluate = boom
        self.addCleanup(lambda: setattr(preflight, "_setup_evaluate", all_steps_ok))
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)


class TestConfigValidityGate(unittest.TestCase):
    def test_invalid_live_mode_is_denied_not_treated_as_autonomous(self):
        """对抗测试原文:live_mode="invalid" 会落进宽松分支拿到 ×1.0。"""
        c = cfg()
        c["execution"]["live_mode"] = "invalid"
        r = run(order(), c, NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("配置合法性", names_failed(r))

    def test_unknown_live_mode_does_not_widen_size(self):
        """第二道:即便配置闸门被绕过,尺寸也按最严档,不按 autonomous。"""
        ex = {"size_scale_observe": 0.4, "size_scale_supported": 1.0,
              "max_order_usd": 100, "live_mode": "invalid"}

        class R:
            id, kind, status, scope = "r1", "market", "supported", "live"

        scale, _ = preflight.rule_size_tier(R(), ex)
        self.assertEqual(scale, 1.0)
        mode = str(ex.get("live_mode", "guarded"))
        self.assertNotEqual(mode, "autonomous")

    def test_negative_cap_is_denied(self):
        c = cfg()
        c["execution"]["max_order_usd"] = -1
        r = run(order(), c, NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertTrue({"配置结构", "配置合法性"} & names_failed(r))


class TestEmergencyExit(unittest.TestCase):
    """★ 风控不能变成"进得去出不来"。"""

    def _closing(self, **over):
        o = order(intent="close", amount_usd=120.0, reason_tag="清仓", **over)
        return o

    def test_liquidation_allowed_after_same_symbol_traded_today(self):
        """对抗测试原文:同一股票当天交易过后,紧急清仓仍被"重复交易"拦截。"""
        o = self._closing(today_orders=[{"symbol": "AAAA", "side": "buy", "amount": 50}])
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, ALLOW, [c.detail for c in r.blockers])

    def test_liquidation_allowed_after_daily_order_count_reached(self):
        """对抗测试原文:达到每日订单数上限后,紧急清仓仍被拦截。"""
        today = [{"symbol": f"S{i}", "side": "buy", "amount": 10} for i in range(6)]
        r = run(self._closing(today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, ALLOW, [c.detail for c in r.blockers])

    def test_normal_sell_still_blocked_by_daily_count(self):
        """豁免只属于 intent=close —— 普通减仓不该顺带获得豁免。"""
        today = [{"symbol": f"S{i}", "side": "buy", "amount": 10} for i in range(6)]
        r = run(order(today_orders=today), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("单日笔数", names_failed(r))

    def test_liquidation_without_position_evidence_is_denied(self):
        """否则任何单子都能自称清仓来绕开上限。"""
        o = self._closing(today_orders=[{"symbol": "AAAA", "side": "buy", "amount": 50}])
        del o["position"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("清仓证据", names_failed(r))

    def test_liquidation_still_bound_by_account_identity(self):
        o = self._closing()
        o["account_id"] = "555000222"          # privacy-allow
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("账户身份", names_failed(r))


class TestShareCount(unittest.TestCase):
    def test_selling_more_shares_than_held_is_denied(self):
        """对抗测试原文:持有 1 股却卖出 2 股,在部分金额条件下仍可能 ALLOW。"""
        o = order(qty=2, price=10.0, amount_usd=None, intent="close",
                  reason_tag="清仓",
                  position={"market_value": 100.0, "qty": 1, "avg_cost": 10.0})
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("持仓股数", names_failed(r))

    def test_selling_exactly_held_shares_is_fine(self):
        o = order(qty=1, price=10.0, amount_usd=None, intent="close",
                  reason_tag="清仓",
                  position={"market_value": 10.0, "qty": 1, "avg_cost": 10.0})
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertNotIn("持仓股数", names_failed(r))


class TestFailClosedOnMissingData(unittest.TestCase):
    def test_buy_without_buying_power_is_denied(self):
        """对抗测试原文:买单缺失 portfolio/BP 数据时可能 fail-open。"""
        o = order(side="buy", reason_tag="建仓", amount_usd=30.0,
                  primary_rule_id=None)
        o.pop("portfolio", None)
        o.pop("primary_rule_id", None)
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("风控数据完整性", names_failed(r))

    def test_broken_ledger_denies_instead_of_passing_silently(self):
        """对抗测试原文:台账损坏时 LedgerError 被吞掉,订单可能继续放行。"""
        def boom():
            from meigu_lib import LedgerError

            raise LedgerError("台账第 3 行列数不对")

        saved = preflight.parse_trades
        preflight.parse_trades = boom
        self.addCleanup(lambda: setattr(preflight, "parse_trades", saved))
        r = run(order(), cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("台账可读", names_failed(r))


class TestRuleIdContract(unittest.TestCase):
    def test_legacy_rule_ids_field_is_rejected_loudly(self):
        """对抗测试原文:文档要求写 rule_ids,代码只认 primary_rule_id,
        结果是静默降到 40% 尺寸 —— 静默降档比报错难查得多。"""
        o = order(side="buy", reason_tag="建仓", amount_usd=20.0,
                  rule_ids=["cash-deployment"])
        o.pop("primary_rule_id", None)
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("证据尺寸", names_failed(r))


class TestSizeFieldConflict(unittest.TestCase):
    """★ 本轮最严重的绕过:两套尺寸口径不一致时,所有金额上限一次全废。"""

    def test_amount_usd_cannot_understate_qty_times_price(self):
        """对抗测试原文:amount_usd=1 而 qty×price=10000,判定 ALLOW。"""
        r = run(order(amount_usd=1, qty=100, price=100, intent="close",
                      reason_tag="清仓", position={"market_value": 10000.0, "qty": 100}),
                cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("订单结构", names_failed(r))

    def test_consistent_two_field_order_is_fine(self):
        o = order(amount_usd=30.0, qty=3, price=10.0,
                  position={"market_value": 120.0, "qty": 12})
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, ALLOW, [c.detail for c in r.blockers])

    def test_amount_uses_the_larger_of_the_two(self):
        """即便校验被绕过,金额也按更严的口径算。"""
        self.assertEqual(preflight._order_amount({"amount_usd": 1, "qty": 100, "price": 100}),
                         10000.0)


class TestRiskDataCompleteness(unittest.TestCase):
    """★ 缺字段不是"这道闸门不适用",而是"这道闸门没跑"。"""

    def _buy(self, **kw):
        o = order(side="buy", reason_tag="建仓", intent="", amount_usd=20.0, **kw)
        o.pop("primary_rule_id", None)
        return o

    def test_buy_missing_total_and_equity_is_denied(self):
        """对抗测试原文:只给 buying_power 就跳过集中度与现金底线。"""
        o = self._buy(portfolio={"buying_power": 90.0})
        o["portfolio"] = {"buying_power": 90.0}
        o.pop("position", None)
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("风控数据完整性", names_failed(r))

    def test_missing_today_orders_is_denied(self):
        """缺 today_orders 会把当日累计、笔数、同标的重复一起清零。"""
        o = order()
        del o["today_orders"]
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("订单结构", names_failed(r))

    def test_negative_today_order_amount_is_denied(self):
        """负数金额可以抵消已用额度。"""
        r = run(order(today_orders=[{"symbol": "B", "side": "buy", "amount": -5000}]),
                cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("订单结构", names_failed(r))


class TestStrictBooleans(unittest.TestCase):
    def test_string_false_does_not_enable_execution(self):
        """★ 非空字符串在 Python 里为真 —— 配置里一个引号就能让总开关常开。"""
        c = cfg()
        c["execution"]["enabled"] = "false"
        r = run(order(), c, NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("配置结构", names_failed(r))

    def test_string_false_dry_run_does_not_become_live(self):
        c = cfg()
        c["execution"]["dry_run"] = "false"
        r = run(order(), c, NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)


class TestNoTracebacksOnBadInput(unittest.TestCase):
    """★ 崩溃不等于拒绝:traceback 容易被当成偶发故障重试。"""

    def test_bad_inputs_all_produce_structured_deny(self):
        cases = {
            "amount_usd 非数字": (dict(amount_usd="oops"), {}),
            "qty 为负": (dict(qty=-5, amount_usd=10.0), {}),
            "qty 为 NaN": (dict(qty=float("nan"), price=1.0, amount_usd=None), {}),
            "today_orders 是字符串": (dict(today_orders="oops"), {}),
            "position 是字符串": (dict(position="oops"), {}),
            "order_type 非法": (dict(order_type="anything"), {}),
            "market_hours 非法": (dict(market_hours="anything"), {}),
            "intent 非法": (dict(intent="whatever"), {}),
            "max_order_usd 非数字": ({}, dict(max_order_usd="oops")),
            "kill_switch 指向仓外": ({}, dict(kill_switch_file="/tmp")),
            "max_orders_per_day 非整数": ({}, dict(max_orders_per_day="six")),
        }
        for name, (omut, cmut) in cases.items():
            with self.subTest(case=name):
                c = cfg()
                c["execution"].update(cmut)
                r = run(order(**omut), c, NOON, vocab=TEST_VOCAB)   # 不得抛异常
                self.assertEqual(r.verdict, DENY, name)


class TestExitQuantityEvidence(unittest.TestCase):
    def test_share_sell_without_position_qty_is_denied(self):
        """对抗测试原文:只给 market_value 不给 qty,卖 100 股仍 ALLOW。"""
        o = order(intent="close", reason_tag="清仓", qty=100, price=1.2,
                  amount_usd=120.0, position={"market_value": 120.0})
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("持仓股数", names_failed(r))

    def test_close_intent_must_actually_close(self):
        """声明清仓却只卖一小部分 = 用清仓通道换取笔数豁免。"""
        o = order(intent="close", reason_tag="清仓", qty=1, price=10.0, amount_usd=10.0,
                  position={"market_value": 100.0, "qty": 10})
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, DENY)
        self.assertIn("清仓数量吻合", names_failed(r))

    def test_genuine_full_close_passes(self):
        o = order(intent="close", reason_tag="清仓", qty=10, price=10.0, amount_usd=100.0,
                  position={"market_value": 100.0, "qty": 10})
        r = run(o, cfg(), NOON, vocab=TEST_VOCAB)
        self.assertEqual(r.verdict, ALLOW, [c.detail for c in r.blockers])


if __name__ == "__main__":
    unittest.main()


