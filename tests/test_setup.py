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


PROFILE_OK = """[account]
id = "555000111"  # privacy-allow(TOML 注释,同时也是逐行豁免标记)

[execution]
enabled = false
dry_run = true
max_order_usd = 80
max_daily_usd = 200
max_orders_per_day = 6
kill_switch_file = "data/HALTED"
live_mode = "guarded"
"""


def sandbox(case, profile: str = PROFILE_OK):
    """把状态机的全部落盘路径挪进临时目录。

    不隔离就会读到本机真实的 data/ 与 config/ —— 测试结论会随开发者本机
    做没做过只读验证而漂移(这个坑已经踩过一次)。
    """
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    (d / "data").mkdir()
    if profile is not None:
        (d / "config" / "profile.toml").write_text(profile, encoding="utf-8")

    saved = {k: getattr(S, k) for k in
             ("CONFIG_DIR", "DATA_DIR", "STATE_FILE", "SALT_FILE", "DRILL_LOG")}
    S.CONFIG_DIR = d / "config"
    S.DATA_DIR = d / "data"
    S.STATE_FILE = d / "data" / "setup-state.json"
    S.SALT_FILE = d / "data" / ".setup-salt"
    S.DRILL_LOG = d / "data" / "drill-runs.jsonl"

    def restore():
        for k, v in saved.items():
            setattr(S, k, v)

    case.addCleanup(restore)
    return d


def stub_prereqs(case):
    """把演练之前的三步打桩成已就绪 —— 这些步骤本身另有用例覆盖。"""
    ok = lambda state: S.Step(state, True, "stub")          # noqa: E731
    saved = (S.check_mcp, S.check_profile, S.check_strategy)
    S.check_mcp = lambda state: ok(S.MCP_CONNECTED_READONLY)
    S.check_profile = lambda: ok(S.PROFILE_READY)
    S.check_strategy = lambda: ok(S.STRATEGY_READY)

    def restore():
        S.check_mcp, S.check_profile, S.check_strategy = saved

    case.addCleanup(restore)


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
        sandbox(self, profile=None)          # 无 profile.toml,跳过一致性比对
        self.tmp = S.STATE_FILE

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
        # 用统一的 sandbox():手工打桩漏掉 SALT_FILE 时,测试会把
        # data/.setup-salt 写进真实仓库 —— 干净 clone 跑完测试就"脏"了。
        sandbox(self, PROFILE_OK.replace("555000111", "111222333"))  # privacy-allow

    def test_mismatched_account_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            S.record_mcp(full_mcp(account_id="555000999"))   # privacy-allow
        self.assertIn("不一致", str(ctx.exception))

    def test_matching_account_is_accepted(self):
        self.assertIn("2333", S.record_mcp(full_mcp(account_id="111222333")))  # privacy-allow


class TestReadonlyDuringVerification(unittest.TestCase):
    """只读验证必须在执行关闭的状态下进行,否则"只读"无从谈起。"""

    def setUp(self):
        sandbox(self, PROFILE_OK.replace("555000111", "111222333")   # privacy-allow
                                .replace("enabled = false", "enabled = true")
                                .replace("dry_run = true", "dry_run = false"))

    def test_live_execution_blocks_readonly_verification(self):
        with self.assertRaises(ConfigError) as ctx:
            S.record_mcp(full_mcp(account_id="111222333"))   # privacy-allow
        self.assertIn("只读验证必须", str(ctx.exception))


class TestDrillRecord(unittest.TestCase):
    """★ 演练不能靠自报。

    旧实现只接受六个布尔值 —— 等于让被检查方出具检查结论。现在要求存在
    **由 preflight 写下的**证据行,run id 由 --start-drill 生成。
    """

    def setUp(self):
        self.dir = sandbox(self)
        stub_prereqs(self)

    def _evidence(self, run_id: str, verdict: str = "DRY_RUN",
                  stages=("preflight", "journal", "review")):
        """模拟各阶段脚本写下的机器证据。

        run id / nonce / 账户指纹都取自当前 active run —— 这正是要验证的绑定关系。
        """
        for stage in stages:
            S.append_drill_evidence(stage, "test")
        if "preflight" in stages:
            lines = S.DRILL_LOG.read_text(encoding="utf-8").splitlines()
            for i, ln in enumerate(lines):
                rec = json.loads(ln)
                if rec.get("stage") == "preflight":
                    rec["verdict"] = verdict
                    lines[i] = json.dumps(rec, ensure_ascii=False)
            S.DRILL_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_drill_with_real_evidence_is_recorded(self):
        rid = S.start_drill()
        self._evidence(rid)
        S.record_drill(full_drill())
        rec = json.loads(S.STATE_FILE.read_text())["drill"]
        self.assertTrue(rec["passed"])
        self.assertEqual(rec["run_id"], rid)

    def test_only_preflight_evidence_is_not_a_full_drill(self):
        """只证明 preflight 跑过,不等于端到端演练跑过。"""
        rid = S.start_drill()
        self._evidence(rid, stages=("preflight",))
        with self.assertRaises(ConfigError) as cm:
            S.record_drill(full_drill())
        self.assertIn("机器证据", str(cm.exception))

    def test_drill_without_start_is_rejected(self):
        """★ 没跑过 --start-drill,自造 run id 也不行 —— 证据链的锚点
        不能由被验证方指定。"""
        S.DRILL_LOG.write_text(
            json.dumps({"run_id": "made-up", "stage": "preflight",
                        "verdict": "DRY_RUN"}) + "\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as cm:
            S.record_drill(full_drill(run_id="made-up"))
        self.assertIn("没有进行中的演练", str(cm.exception))

    def test_same_run_cannot_be_recorded_twice(self):
        rid = S.start_drill()
        self._evidence(rid)
        S.record_drill(full_drill())
        with self.assertRaises(ConfigError):
            S.record_drill(full_drill(run_id=rid))

    def test_string_false_is_not_true(self):
        """★ 非空字符串在 Python 里为真 —— 必须严格比 True。"""
        rid = S.start_drill()
        self._evidence(rid)
        with self.assertRaises(ConfigError):
            S.record_drill({k: "false" for k in S.DRILL_CHECKS})

    def test_booleans_alone_are_not_enough(self):
        """六个 true 但从没跑过任何脚本 —— 必须拒绝。"""
        S.start_drill()
        with self.assertRaises(ConfigError) as cm:
            S.record_drill(full_drill())
        self.assertIn("证据", str(cm.exception))

    def test_evidence_must_be_a_dry_run_verdict(self):
        rid = S.start_drill()
        self._evidence(rid, verdict="DENY")
        with self.assertRaises(ConfigError):
            S.record_drill(full_drill())

    def test_drill_cannot_be_started_in_live_mode(self):
        (S.CONFIG_DIR / "profile.toml").write_text(
            PROFILE_OK.replace("enabled = false", "enabled = true")
                      .replace("dry_run = true", "dry_run = false"),
            encoding="utf-8")
        with self.assertRaises(ConfigError):
            S.start_drill()

    def test_drill_cannot_be_recorded_in_live_mode(self):
        """对抗测试原文:enabled=true, dry_run=false 下仍能记成"演练完成"。"""
        rid = S.start_drill()
        self._evidence(rid)
        (S.CONFIG_DIR / "profile.toml").write_text(
            PROFILE_OK.replace("enabled = false", "enabled = true")
                      .replace("dry_run = true", "dry_run = false"),
            encoding="utf-8")
        with self.assertRaises(ConfigError):
            S.record_drill(full_drill())

    def test_incomplete_drill_rejected(self):
        rid = S.start_drill()
        self._evidence(rid)
        for key in S.DRILL_CHECKS:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    S.record_drill(full_drill(**{key: False}))

    def test_run_id_mismatch_rejected(self):
        rid = S.start_drill()
        self._evidence(rid)
        with self.assertRaises(ConfigError):
            S.record_drill(full_drill(run_id="deadbeef"))


class TestDrillRetry(unittest.TestCase):
    """★ 演练本来就是"跑一遍、发现问题、修好、再跑一遍"。

    只要历史上出现过一次 ok=false 就永久否决同一个 run,等于逼人重开演练。
    每个环节只采信**最后一条**证据。
    """

    def setUp(self):
        self.dir = sandbox(self)
        stub_prereqs(self)

    def test_failed_stage_then_success_is_accepted(self):
        S.start_drill()
        S.append_drill_evidence("preflight", "x", ok=True, verdict="DRY_RUN")
        S.append_drill_evidence("journal", "3 处结构错误", ok=False)
        S.append_drill_evidence("journal", "修好了", ok=True)     # 重跑并通过
        S.append_drill_evidence("review", "x", ok=True)
        S.record_drill(full_drill())
        self.assertTrue(json.loads(S.STATE_FILE.read_text())["drill"]["passed"])

    def test_success_then_failure_is_still_rejected(self):
        """顺序反过来:最后一次是失败,就不算通过。"""
        S.start_drill()
        S.append_drill_evidence("preflight", "x", ok=True, verdict="DRY_RUN")
        S.append_drill_evidence("journal", "先通过了", ok=True)
        S.append_drill_evidence("journal", "又坏了", ok=False)
        S.append_drill_evidence("review", "x", ok=True)
        with self.assertRaises(ConfigError) as cm:
            S.record_drill(full_drill())
        self.assertIn("最近一次", str(cm.exception))


class TestPrerequisitesForDrill(unittest.TestCase):
    def setUp(self):
        self.dir = sandbox(self)

    def test_drill_requires_mcp_first(self):
        """没做只读验证就记演练 —— 顺序必须被强制,不只是被展示。"""
        with self.assertRaises(ConfigError) as cm:
            S.record_drill(full_drill())
        self.assertIn("前置步骤", str(cm.exception))


class TestStrictTruth(unittest.TestCase):
    """★ 对抗测试原文:九项检查全部提交字符串 "false",系统仍记录"验证通过"。"""

    def setUp(self):
        self.dir = sandbox(self)

    def test_string_false_does_not_pass_mcp_checks(self):
        with self.assertRaises(ConfigError):
            S.record_mcp({k: "false" for k in S.MCP_CHECKS}
                         | {"account_id": "555000111"})     # privacy-allow

    def test_truthy_non_bool_does_not_pass(self):
        with self.assertRaises(ConfigError):
            S.record_mcp({k: 1 for k in S.MCP_CHECKS}
                         | {"account_id": "555000111"})     # privacy-allow


class TestAccountFingerprint(unittest.TestCase):
    """★ 换了账户,之前的验证就必须失效。

    对抗测试原文:先为尾号 2222 的账户记录验证,再把 profile 换成尾号 3333,
    系统依然认为验证有效。只存后 4 位甚至连"尾号相同的另一个账户"都分不出来。
    """

    def setUp(self):
        self.dir = sandbox(self)

    def test_fingerprint_is_not_reversible_to_account_id(self):
        acct = "555000111"                                   # privacy-allow
        fp = S.account_fingerprint(acct)
        self.assertNotIn(acct, fp)
        self.assertEqual(len(fp), 64)

    def test_same_account_same_fingerprint(self):
        self.assertEqual(S.account_fingerprint("555000111"),  # privacy-allow
                         S.account_fingerprint("555000111"))  # privacy-allow

    def test_changing_account_invalidates_mcp_verification(self):
        S.record_mcp(full_mcp())
        self.assertTrue(S.check_mcp(S._load_state()).ok)

        (S.CONFIG_DIR / "profile.toml").write_text(
            PROFILE_OK.replace("555000111", "555000222"),     # privacy-allow
            encoding="utf-8")
        step = S.check_mcp(S._load_state())
        self.assertFalse(step.ok)
        self.assertIn("失效", step.detail)

    def test_changing_account_invalidates_drill(self):
        stub_prereqs(self)
        rid = S.start_drill()
        for stage in ("preflight", "journal", "review"):
            S.append_drill_evidence(stage, "test")
        lines = S.DRILL_LOG.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[0]); rec["verdict"] = "DRY_RUN"
        lines[0] = json.dumps(rec, ensure_ascii=False)
        S.DRILL_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        S.record_drill(full_drill())
        self.assertTrue(S.check_automation(S._load_state()).ok)

        (S.CONFIG_DIR / "profile.toml").write_text(
            PROFILE_OK.replace("555000111", "555000222"),     # privacy-allow
            encoding="utf-8")
        self.assertFalse(S.check_automation(S._load_state()).ok)


class TestExecutionValidation(unittest.TestCase):
    """存在 ≠ 合法。一个负数上限等于没有上限。"""

    def _cfg(self, **over):
        ex = {"max_order_usd": 80, "max_daily_usd": 200, "max_orders_per_day": 6,
              "kill_switch_file": "data/HALTED", "live_mode": "guarded"}
        ex.update(over)
        return {"execution": ex}

    def test_valid_config_passes(self):
        self.assertEqual(S.validate_execution(self._cfg()), [])

    def test_negative_amount_rejected(self):
        self.assertTrue(S.validate_execution(self._cfg(max_order_usd=-1)))

    def test_zero_order_count_rejected(self):
        self.assertTrue(S.validate_execution(self._cfg(max_orders_per_day=0)))

    def test_non_numeric_cap_rejected(self):
        self.assertTrue(S.validate_execution(self._cfg(max_daily_usd="200")))

    def test_bool_is_not_a_number(self):
        self.assertTrue(S.validate_execution(self._cfg(max_order_usd=True)))

    def test_scale_above_one_rejected(self):
        self.assertTrue(S.validate_execution(self._cfg(size_scale_supported=1.5)))

    def test_invalid_live_mode_rejected(self):
        self.assertTrue(S.validate_execution(self._cfg(live_mode="invalid")))

    def test_kill_switch_outside_repo_rejected(self):
        self.assertTrue(S.validate_execution(self._cfg(kill_switch_file="../HALTED")))
        self.assertTrue(S.validate_execution(self._cfg(kill_switch_file="/tmp/HALTED")))


class TestAtomicStateWrite(unittest.TestCase):
    def setUp(self):
        self.dir = sandbox(self)

    def test_state_write_leaves_no_partial_file(self):
        S._save_state({"a": 1})
        self.assertEqual(json.loads(S.STATE_FILE.read_text())["a"], 1)
        self.assertFalse(list(S.DATA_DIR.glob("*.tmp")))

    def test_corrupt_state_does_not_crash(self):
        S.STATE_FILE.write_text("{ broken", encoding="utf-8")
        self.assertEqual(S._load_state(), {})


class TestAuthorizeWriteVerification(unittest.TestCase):
    """★ 不能"提示成功但实际没开"。"""

    def setUp(self):
        self.dir = sandbox(self)
        steps = [S.Step(s, True, "stub") for s in S.ORDER]
        saved = S.evaluate
        S.evaluate = lambda: (steps, S.ORDER[-1])
        self.addCleanup(lambda: setattr(S, "evaluate", saved))

    def _ex(self):
        import tomllib
        with (S.CONFIG_DIR / "profile.toml").open("rb") as fh:
            return tomllib.load(fh)["execution"]

    def test_fields_are_updated(self):
        S.authorize_live("guarded")
        ex = self._ex()
        self.assertEqual((ex["enabled"], ex["dry_run"], ex["live_mode"]),
                         (True, False, "guarded"))

    def test_missing_fields_are_inserted_not_silently_skipped(self):
        """对抗测试原文:profile 缺 enabled/dry_run 时,正则替换命中零次,却报成功。"""
        (S.CONFIG_DIR / "profile.toml").write_text(
            '[account]\nid = "555000111"\n\n[execution]\n'      # privacy-allow
            'max_order_usd = 80\n', encoding="utf-8")
        S.authorize_live("autonomous")
        ex = self._ex()
        self.assertEqual((ex["enabled"], ex["dry_run"], ex["live_mode"]),
                         (True, False, "autonomous"))
        self.assertEqual(ex["max_order_usd"], 80)

    def test_result_is_read_back_and_verified(self):
        S.authorize_live("guarded")
        self.assertTrue(S.check_live().ok)


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
