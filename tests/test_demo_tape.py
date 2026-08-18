"""演示录制的隐私守卫。

`docs/demo.gif` 会被提交进**公开**仓库。gif 是像素,提交后无法审计内容,
也无法像文本那样被 check_privacy.py 扫描 —— 所以必须在**生成之前**约束它:
录制脚本只允许跑 demo 模式与 demo/ 下的虚构固件。

这是 v5.0.0 那次泄漏的同一类问题(见 CHANGELOG):
一个无法被事后检查的产物,必须在生产环节就把输入限死。
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ROOT = Path(__file__).resolve().parent.parent
TAPE = ROOT / "demo" / "demo.tape"

# 录制脚本里允许出现的命令(前缀匹配)。加新条目前先问:它会读真实数据吗?
ALLOWED_COMMAND_PREFIXES = (
    "make dashboard-demo",
    "python3 scripts/dashboard.py --demo",
    "python3 scripts/preflight.py --order-file demo/",
    "python3 scripts/stats.py --file examples/",
    "python3 scripts/trading_day.py",
    "clear",
    "export PS1=",
    "# ",                      # 纯注释行,只是给观众看的
    "    --now-et",            # 上一行的续行
)

# 绝不允许出现在录制脚本里的字样 —— 它们意味着会读到真实的用户层数据
FORBIDDEN_SUBSTRINGS = (
    "data/",
    "reports/",
    "config/profile.toml",
    "config/watchlist.toml",
    "make dashboard\n",        # 不带 -demo 的版本读 data/
    "make stats",              # 读 data/trades.tsv
    "make report",             # 写 reports/
    "make doctor",             # 会打印本机环境
    "make journal",
    ".claude/settings.local.json",
    "/Users/",
)

TYPE_RE = re.compile(r'^\s*Type\s+"(.*)"\s*$')


def tape_text() -> str:
    return TAPE.read_text(encoding="utf-8")


def typed_payloads() -> list[str]:
    """所有 `Type "..."` 的内容 —— 这才是真正会被执行/显示的东西。"""
    return [m.group(1) for m in (TYPE_RE.match(ln) for ln in tape_text().splitlines()) if m]


def typed_commands() -> list[str]:
    """排除单键输入(`Type "s"` 这类是按键,不是命令)。"""
    return [c for c in typed_payloads() if len(c.strip()) > 2 or " " in c]


class TestTapeExists(unittest.TestCase):
    def test_tape_present(self):
        self.assertTrue(TAPE.exists(), "demo/demo.tape 缺失")

    def test_output_goes_to_docs(self):
        self.assertIn("Output docs/demo.gif", tape_text())

    def test_deterministic_size_is_pinned(self):
        """尺寸/帧率必须写死,否则每次录出来的 gif 都不一样,无法复现。"""
        for setting in ("Set Width", "Set Height", "Set FontSize", "Set Framerate"):
            self.assertIn(setting, tape_text(), f"缺少 {setting}")


class TestTapeUsesOnlyDemoData(unittest.TestCase):
    def test_no_forbidden_paths(self):
        # 只检查**会被执行的内容**,不检查注释 ——
        # 注释里说明"为什么不能读 data/"是好事,不该被自己的守卫拦下。
        executed = "\n".join(typed_payloads()) + "\n"
        found = [s for s in FORBIDDEN_SUBSTRINGS if s in executed]
        self.assertEqual(
            found, [],
            f"录制脚本里出现了会读到真实数据的字样:{found}。"
            "gif 进公开仓后无法审计内容,必须只用 demo 固件。",
        )

    def test_single_keystrokes_are_not_treated_as_commands(self):
        """`Type "s"` / `Type "q"` 是按键,不是命令 —— 不该要求它们进白名单。"""
        self.assertIn("s", typed_payloads())
        self.assertNotIn("s", typed_commands())

    def test_every_typed_command_is_allowlisted(self):
        bad = [
            c for c in typed_commands()
            if not any(c.startswith(p) for p in ALLOWED_COMMAND_PREFIXES)
        ]
        self.assertEqual(
            bad, [],
            f"以下命令不在演示白名单里:{bad}\n"
            f"加进 ALLOWED_COMMAND_PREFIXES 前先确认它不会读真实数据。",
        )

    def test_dashboard_is_invoked_in_demo_mode(self):
        cmds = " ".join(typed_commands())
        self.assertTrue(
            "dashboard-demo" in cmds or "dashboard.py --demo" in cmds,
            "录制必须用 demo 模式启动仪表盘",
        )


class TestDemoFixtures(unittest.TestCase):
    def test_referenced_fixtures_exist(self):
        for cmd in typed_commands():
            for token in cmd.split():
                if token.startswith("demo/") or token.startswith("examples/"):
                    self.assertTrue((ROOT / token).exists(), f"固件缺失:{token}")

    def test_order_fixture_is_valid_and_placeholder_account(self):
        from preflight import validate_order

        payload = json.loads(
            (ROOT / "demo" / "order-oversized-trim.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_order(payload), [])
        self.assertEqual(payload["account_id"], "000000000",
                         "演示固件必须用占位账户号")

    def test_order_fixture_actually_triggers_the_gate(self):
        """演示的意义在于闸门真的拦住 —— 固件若不再触发,gif 就在说谎。"""
        from preflight import DENY, run

        payload = json.loads(
            (ROOT / "demo" / "order-oversized-trim.json").read_text(encoding="utf-8"))
        cfg = {
            "account": {"id": "000000000"},
            "execution": {"enabled": True, "dry_run": False, "max_order_usd": 80,
                          "max_daily_usd": 200, "max_orders_per_day": 6,
                          "intent_ttl_minutes": 15, "quote_max_age_minutes": 10,
                          "kill_switch_file": "data/__no_halt__"},
            "trade": {"size_std": 50},
            "position": {"max_single_pct": 50, "reduce_pct_warn": 50,
                         "residual_threshold_ratio": 0.5},
            "cash": {"floor_pct": 15},
        }
        import datetime as dt
        from zoneinfo import ZoneInfo

        now = dt.datetime(2026, 8, 18, 13, 5, tzinfo=ZoneInfo("America/New_York"))
        result = run(payload, cfg, now)
        self.assertEqual(result.verdict, DENY)
        self.assertIn("减仓占比", {c.name for c in result.blockers})

    def test_portfolio_fixture_has_no_real_looking_account(self):
        import check_privacy

        problems = check_privacy.check(
            ["examples/sample-portfolio.json", "demo/order-oversized-trim.json"])
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
