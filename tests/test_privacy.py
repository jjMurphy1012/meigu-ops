"""隐私检查测试 —— 这是数据分层契约的最后一道防线,必须真的能拦住东西。"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_privacy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestForbiddenPaths(unittest.TestCase):
    def test_flags_real_profile_config(self):
        problems = check_privacy.check(["config/profile.toml"])
        self.assertTrue(any("用户层文件被纳入版本控制" in p for p in problems))

    def test_flags_settings_local(self):
        problems = check_privacy.check([".claude/settings.local.json"])
        self.assertTrue(any("用户层文件被纳入版本控制" in p for p in problems))

    def test_flags_data_directory_content(self):
        problems = check_privacy.check(["data/journal.md"])
        self.assertTrue(any("只允许提交 .gitkeep" in p for p in problems))

    def test_flags_reports_content(self):
        problems = check_privacy.check(["reports/2026-08-17.md"])
        self.assertTrue(any("只允许提交 .gitkeep" in p for p in problems))

    def test_allows_gitkeep(self):
        problems = check_privacy.check(["data/.gitkeep", "reports/.gitkeep"])
        self.assertEqual(problems, [])

    def test_allows_example_config(self):
        problems = check_privacy.check(["config/profile.example.toml"])
        self.assertEqual(problems, [])


class TestContentPatterns(unittest.TestCase):
    """用临时文件验证内容模式,避免依赖仓库当前内容。"""

    def _check_content(self, content: str, filename: str = "modes/_tmp_test.md") -> list[str]:
        path = ROOT / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        try:
            return check_privacy.check([filename])
        finally:
            path.unlink(missing_ok=True)

    def test_flags_nine_digit_account_id(self):
        problems = self._check_content("下单账户 555000123,只露后 4 位。\n")  # privacy-allow
        self.assertTrue(any("brokerage_account_id" in p for p in problems))

    def test_allows_placeholder_account_id(self):
        problems = self._check_content('id = "000000000"\n')
        self.assertFalse(any("brokerage_account_id" in p for p in problems))

    def test_allows_account_id_placeholder_token(self):
        problems = self._check_content("账户号见 config/profile.toml,文中写 {{account_id}}。\n")
        self.assertEqual(problems, [])

    def test_allows_last_four_digits(self):
        problems = self._check_content("Agentic 子账户(后 4 位 4351)\n")
        self.assertEqual(problems, [])

    def test_flags_absolute_home_path(self):
        problems = self._check_content("PROJECT_DIR=/Users/someuser/Desktop/x\n")  # privacy-allow
        self.assertTrue(any("absolute_home_path" in p for p in problems))

    def test_flags_real_email(self):
        problems = self._check_content("联系 someone@university.edu\n")  # privacy-allow
        self.assertTrue(any("personal_email" in p for p in problems))

    def test_allows_example_email(self):
        problems = self._check_content("联系 you@example.com\n")
        self.assertEqual(problems, [])

    def test_flags_github_token(self):
        problems = self._check_content("token=ghp_abcdefghijklmnopqrstuvwxyz0123\n")  # privacy-allow
        self.assertTrue(any("generic_token" in p for p in problems))

    def test_flags_anthropic_key(self):
        problems = self._check_content("key=sk-ant-abcdefghijklmnopqrstuvwxyz\n")  # privacy-allow
        self.assertTrue(any("generic_token" in p for p in problems))

    def test_flags_aws_key(self):
        problems = self._check_content("AKIAIOSFODNN7EXAMPLE\n")  # privacy-allow
        self.assertTrue(any("aws_key" in p for p in problems))

    def test_reports_line_number(self):
        problems = self._check_content("第一行\n第二行\n账户 555000123\n")  # privacy-allow
        self.assertTrue(any(":3:" in p for p in problems))

    def test_pattern_definitions_do_not_self_match(self):
        """check_privacy.py 自身要讲解模式,不能自我误报。"""
        problems = check_privacy.check(["scripts/check_privacy.py"])
        self.assertEqual(problems, [])


class TestPragmaGranularity(unittest.TestCase):
    """回归测试:2026-08-18 的真实泄漏 —— 整文件白名单把真实账户号带上了公开仓。

    根因是豁免粒度错了:整文件白名单会连同该文件**未来新增的每一行**一起豁免。
    这组测试锁住"逐行豁免"的设计,不让它退回整文件白名单。
    """

    def _check_content(self, content: str, filename: str = "modes/_tmp_pragma.md") -> list[str]:
        path = ROOT / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        try:
            return check_privacy.check([filename])
        finally:
            path.unlink(missing_ok=True)

    def test_pragma_exempts_its_own_line(self):
        problems = self._check_content("账户 555000123 <!-- privacy-allow -->\n")
        self.assertEqual(problems, [])

    def test_pragma_does_not_exempt_other_lines(self):
        """关键断言:一行豁免不能顺带豁免下一行。"""
        # 注意:字符串里的 "privacy-allow" 决定被扫描的临时文件哪一行放行;
        # 行尾的 `# privacy-allow` 决定本源文件这一行放行 —— 两者是不同层面。
        content = (
            "账户 555000123 <!-- privacy-allow -->\n"  # 内容已标注 → 放行
            "账户 555000456\n"  # privacy-allow · 内容未标注 → 必须被抓
        )
        problems = self._check_content(content)
        self.assertTrue(any(":2:" in p for p in problems), problems)
        self.assertFalse(any(":1:" in p for p in problems), problems)

    def test_no_file_level_allowlist_exists(self):
        """设计守卫:一旦有人重新引入整文件白名单,这条就会失败。"""
        self.assertFalse(
            hasattr(check_privacy, "ALLOWLIST_FILES"),
            "不要重新引入整文件白名单 —— 它会豁免该文件未来新增的所有行,"
            "这正是 2026-08-18 真实账户号泄漏到公开仓的根因。请用逐行 `privacy-allow`。",
        )

    def test_test_fixture_file_is_scanned_and_clean(self):
        """本文件必须**仍在扫描范围内**,且当前干净 —— 只靠逐行 pragma 放行固件。

        首版的失败正是本文件被整体豁免。这条断言让"本文件被扫描"成为不变量:
        任何未标注的敏感字面量(包括真实账户号)都会让它失败。
        """
        problems = check_privacy.check(["tests/test_privacy.py"])
        self.assertEqual(problems, [], "\n".join(problems))


class TestRepositoryIsClean(unittest.TestCase):
    def test_current_repo_passes(self):
        """整个仓库当前状态必须通过隐私检查 —— 这条失败就说明真的漏了东西。"""
        problems = check_privacy.check(check_privacy.all_files())
        self.assertEqual(problems, [], "\n".join(problems))


class TestScannersAgree(unittest.TestCase):
    """本地扫描器与 CI 历史扫描器不得对同一行给出相反结论。

    2026-08-18 踩过:本地 check_privacy.py 认 `# privacy-allow` 逐行豁免,
    CI 的 git 历史扫描不认 —— 本地全绿、推上去 CI 红,而且历史一旦进去就
    只能重写历史才能变绿。豁免一行的代价因此不是"跳过一次检查",
    而是"这一行永久卡住 CI"。

    所以规则是:**被 pragma 豁免的 9 位数字,必须同时是 CI 认可的占位形态。**
    pragma 只能用来豁免占位值,不能用来夹带真实号 —— 后者仍会被 CI 拦下。
    """

    def _ci_placeholder_re(self) -> re.Pattern:
        """从 workflow 文件里解析出 CI 用的占位值正则 —— 不复制一份。

        复制等于制造第二个真相来源:改了 CI 没改测试,这个测试就在保护一个
        已经不存在的规则。
        """
        wf = (ROOT / ".github/workflows/no-user-data.yml").read_text(encoding="utf-8")
        m = re.search(r"^\s*PLACEHOLDERS='([^']+)'", wf, re.M)
        self.assertIsNotNone(m, "workflow 里找不到 PLACEHOLDERS —— 变量名改了就得同步这里")
        return re.compile(m.group(1))

    def test_pragma_exempted_digits_are_ci_placeholders(self):
        ci_ok = self._ci_placeholder_re()
        nine = re.compile(r"(?<![0-9])[0-9]{9}(?![0-9])")
        offenders = []
        for rel in check_privacy.tracked_files():
            path = ROOT / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if check_privacy.PRAGMA not in line:
                    continue
                for val in nine.findall(line):
                    if not ci_ok.search(val):
                        offenders.append(f"{rel}:{i}:{val}")
        self.assertEqual(
            offenders, [],
            "这些行被 pragma 豁免,但不是 CI 认可的占位形态 —— "
            "本地会过、CI 会红:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
