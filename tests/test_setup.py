"""首次接入状态机测试。

核心不变量:**连接成功 ≠ 获得真钱执行权限**。
这两件事必须分开,而且授权必须发生在验证与演练之后。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import setup as S  # noqa: E402
from meigu_lib import ConfigError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def full_mcp(**over):
    p = {k: True for k in S.MCP_CHECKS}
    p["account_id"] = "555000111"          # privacy-allow
    p.update(over)
    return p


def full_drill(**over):
    p = {k: True for k in S.DRILL_CHECKS}
    p.update(over)
    return p


class TestChecklistCompleteness(unittest.TestCase):
    def test_mcp_checklist_covers_the_acceptance_criteria(self):
        """验收标准是**真的读到数据**,不是"检测到配置文件"。"""
        for key in ("accounts_listed", "target_account_found", "target_account_unique",
                    "positions_readable", "buying_power_readable",
                    "quote_with_timestamp", "review_order_available",
                    "place_order_not_called", "fail_closed_on_error"):
            self.assertIn(key, S.MCP_CHECKS)

    def test_drill_covers_the_full_loop(self):
        for key in ("premarket_ran", "check_ran", "preflight_ran",
                    "order_simulated", "journal_written", "review_ran"):
            self.assertIn(key, S.DRILL_CHECKS)

    def test_place_order_must_be_asserted_not_called(self):
        """只读验证阶段绝不能下单 —— 这一项必须是验收项之一。"""
        self.assertIn("place_order_not_called", S.MCP_CHECKS)


class TestMcpRecordValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "setup-state.json"
        self._orig = S.STATE_FILE
        S.STATE_FILE = self.tmp
        self._orig_cfg = S.CONFIG_DIR
        S.CONFIG_DIR = Path(tempfile.mkdtemp())   # 无 profile.toml,跳过一致性比对

    def tearDown(self):
        S.STATE_FILE = self._orig
        S.CONFIG_DIR = self._orig_cfg

    def test_all_true_is_recorded(self):
        msg = S.record_mcp(full_mcp())
        self.assertIn("0111", msg)
        self.assertTrue(json.loads(self.tmp.read_text())["mcp_check"]["passed"])

    def test_any_missing_check_is_rejected(self):
        for key in S.MCP_CHECKS:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    S.record_mcp(full_mcp(**{key: False}))

    def test_place_order_called_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            S.record_mcp(full_mcp(place_order_not_called=False))
        self.assertIn("place_order_not_called", str(ctx.exception))

    def test_missing_account_id_rejected(self):
        p = full_mcp()
        del p["account_id"]
        with self.assertRaises(ConfigError):
            S.record_mcp(p)

    def test_nothing_written_when_rejected(self):
        with self.assertRaises(ConfigError):
            S.record_mcp(full_mcp(accounts_listed=False))
        self.assertFalse(self.tmp.exists())


class TestAccountMismatch(unittest.TestCase):
    """读到的账户与配置不一致时必须拒绝 —— 不能替用户挑一个。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "profile.toml").write_text(
            '[account]\nid = "111222333"\n[execution]\nenabled = false\ndry_run = true\n',  # privacy-allow
            encoding="utf-8")
        self._c, S.CONFIG_DIR = S.CONFIG_DIR, d
        self._s, S.STATE_FILE = S.STATE_FILE, Path(tempfile.mkdtemp()) / "s.json"

    def tearDown(self):
        S.CONFIG_DIR, S.STATE_FILE = self._c, self._s

    def test_mismatched_account_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            S.record_mcp(full_mcp(account_id="555000999"))   # privacy-allow
        self.assertIn("不一致", str(ctx.exception))

    def test_matching_account_is_accepted(self):
        self.assertIn("2333", S.record_mcp(full_mcp(account_id="111222333")))  # privacy-allow


class TestReadonlyDuringVerification(unittest.TestCase):
    """只读验证必须在执行关闭的状态下进行,否则"只读"无从谈起。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "profile.toml").write_text(
            '[account]\nid = "111222333"\n[execution]\nenabled = true\ndry_run = false\n',  # privacy-allow
            encoding="utf-8")
        self._c, S.CONFIG_DIR = S.CONFIG_DIR, d
        self._s, S.STATE_FILE = S.STATE_FILE, Path(tempfile.mkdtemp()) / "s.json"

    def tearDown(self):
        S.CONFIG_DIR, S.STATE_FILE = self._c, self._s

    def test_live_execution_blocks_readonly_verification(self):
        with self.assertRaises(ConfigError) as ctx:
            S.record_mcp(full_mcp(account_id="111222333"))   # privacy-allow
        self.assertIn("只读验证必须", str(ctx.exception))


class TestDrillRecord(unittest.TestCase):
    def setUp(self):
        self._s, S.STATE_FILE = S.STATE_FILE, Path(tempfile.mkdtemp()) / "s.json"

    def tearDown(self):
        S.STATE_FILE = self._s

    def test_full_drill_recorded(self):
        S.record_drill(full_drill())
        self.assertTrue(json.loads(S.STATE_FILE.read_text())["drill"]["passed"])

    def test_incomplete_drill_rejected(self):
        for key in S.DRILL_CHECKS:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    S.record_drill(full_drill(**{key: False}))


class TestAuthorizationGating(unittest.TestCase):
    """★ 最重要的一条:授权必须发生在验证与演练之后。"""

    def test_cli_refuses_without_approved(self):
        code = S.main(["--authorize-live", "guarded"])
        self.assertEqual(code, 1)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ConfigError):
            S.authorize_live("whatever")

    def test_authorize_blocked_when_prerequisites_missing(self):
        """前置未完成时不得开启真钱 —— 这是整个状态机的意义。"""
        self._s, S.STATE_FILE = S.STATE_FILE, Path(tempfile.mkdtemp()) / "s.json"
        try:
            with self.assertRaises(ConfigError) as ctx:
                S.authorize_live("guarded")
            self.assertIn("前置步骤未完成", str(ctx.exception))
        finally:
            S.STATE_FILE = self._s


class TestStateIsComputedNotRemembered(unittest.TestCase):
    def test_states_are_ordered(self):
        self.assertEqual(S.ORDER[0], S.UNINITIALIZED)
        self.assertEqual(S.ORDER[-1], S.LIVE_AUTHORIZED)
        self.assertEqual(S.ORDER.index(S.MCP_CONNECTED_READONLY), 1)

    def test_mcp_precedes_profile_and_strategy(self):
        """连接要早:券商只读验证排在配置与策略之前。"""
        self.assertLess(S.ORDER.index(S.MCP_CONNECTED_READONLY),
                        S.ORDER.index(S.PROFILE_READY))
        self.assertLess(S.ORDER.index(S.MCP_CONNECTED_READONLY),
                        S.ORDER.index(S.STRATEGY_READY))

    def test_live_is_last(self):
        """授权要晚:真钱执行排在演练之后。"""
        self.assertLess(S.ORDER.index(S.AUTOMATION_READY),
                        S.ORDER.index(S.LIVE_AUTHORIZED))

    def test_evaluate_returns_a_step_per_state(self):
        steps, current = S.evaluate()
        self.assertEqual([s.state for s in steps], S.ORDER)
        self.assertIn(current, S.ORDER)


if __name__ == "__main__":
    unittest.main()
