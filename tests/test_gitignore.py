"""`.gitignore` 集成测试 —— 直接问 git,不做正则推断。

**这个文件的存在理由是一次真实事故:**
2026-08-18 首版 `.gitignore` 里写的是 `config/profile.yml`,而项目实际用的是
`config/profile.toml`。83 个单元测试全绿,但没有一个测试去问过 git
"这个路径真的被忽略了吗"。真实账户配置整整一版没有被保护。

教训:**契约里声明"这些文件永不进仓",就必须有测试去验证 git 的实际行为**,
而不是验证 `.gitignore` 的文本长什么样。
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ROOT = Path(__file__).resolve().parent.parent

# 契约要求:这些路径必须被 git 忽略(AGENTS.md §1 / docs/DATA_CONTRACT.md §1)
MUST_BE_IGNORED = [
    # 真实配置 —— 账户号与下单授权都在这里
    "config/profile.toml",
    "config/watchlist.toml",
    # 策略层:标签词表与纪律规则就是策略本身
    "config/reason-tags.toml",
    "config/rules.toml",
    # 散文形式的纪律手册
    "modes/_strategy.md",
    # 后缀换了也必须继续被保护
    "config/profile.yml",
    "config/profile.yaml",
    "config/profile.json",
    "config/watchlist.yml",
    # 用户层运行时数据
    "data/trades.tsv",
    "data/journal.md",
    "data/lessons.md",
    "data/snapshots/2026-08-18.json",
    "data/_archive/anything.md",
    "reports/2026-08-18.md",
    # CLI 本地配置与凭证
    ".claude/settings.local.json",
    ".mcp.json",
    ".env",
    "web/.env.local",
    # Python 产物
    "__pycache__/x.pyc",
    ".venv/lib/x.py",
]

# 契约要求:这些必须**不**被忽略(否则系统层文件进不了仓)
MUST_NOT_BE_IGNORED = [
    "config/profile.example.toml",
    "config/watchlist.example.toml",
    "config/reason-tags.example.toml",
    "config/rules.example.toml",
    "modes/_strategy.example.md",
    "modes/_mechanics.md",
    "examples/sample-rules.toml",
    "examples/sample-reason-tags.toml",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "Makefile",
    "modes/_mechanics.md",
    "modes/trade.md",
    "scripts/meigu_lib.py",
    "scripts/check_privacy.py",
    "templates/daily-report.md",
    "tests/test_gitignore.py",
    "docs/DATA_CONTRACT.md",
    "examples/sample-trades.tsv",
    ".github/workflows/no-user-data.yml",
    "data/.gitkeep",
    "data/snapshots/.gitkeep",
    "reports/.gitkeep",
]


def git_ignores(path: str) -> bool:
    """直接问 git 是否忽略某路径。文件不需要真实存在。"""
    res = subprocess.run(
        ["git", "-c", "core.quotepath=false", "check-ignore", "-q", "--no-index", path],
        cwd=ROOT,
        capture_output=True,
    )
    # 0 = 被忽略, 1 = 未被忽略, 128 = 不是 git 仓库
    if res.returncode == 128:
        raise unittest.SkipTest("不在 git 仓库内,跳过 gitignore 集成测试")
    return res.returncode == 0


class TestUserLayerIsIgnored(unittest.TestCase):
    def test_all_user_layer_paths_are_ignored(self):
        missed = [p for p in MUST_BE_IGNORED if not git_ignores(p)]
        self.assertEqual(
            missed,
            [],
            "以下用户层路径**没有**被 .gitignore 覆盖,存在泄漏风险:\n  "
            + "\n  ".join(missed),
        )

    def test_real_config_is_ignored_regardless_of_extension(self):
        """后缀从 yml 换成 toml 这类改动不得让保护失效 —— 这正是首版的失败方式。"""
        for ext in ("toml", "yml", "yaml", "json", "ini", "conf"):
            with self.subTest(ext=ext):
                self.assertTrue(
                    git_ignores(f"config/profile.{ext}"),
                    f"config/profile.{ext} 未被忽略",
                )


class TestSystemLayerIsCommittable(unittest.TestCase):
    def test_system_layer_paths_are_not_ignored(self):
        wrongly = [p for p in MUST_NOT_BE_IGNORED if git_ignores(p)]
        self.assertEqual(
            wrongly,
            [],
            "以下系统层路径被 .gitignore 误伤,将无法提交:\n  " + "\n  ".join(wrongly),
        )


class TestNoUserLayerIsTracked(unittest.TestCase):
    def test_git_tracks_no_forbidden_path(self):
        """已被 git 跟踪的文件里不得出现用户层路径。"""
        res = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            self.skipTest("不在 git 仓库内")
        tracked = set(res.stdout.split())

        forbidden = {
            "config/profile.toml",
            "config/watchlist.toml",
            "config/reason-tags.toml",
            "config/rules.toml",
            "modes/_strategy.md",
            ".claude/settings.local.json",
            ".mcp.json",
            ".env",
        }
        self.assertEqual(tracked & forbidden, set())

        leaked = {
            f
            for f in tracked
            if (f.startswith("data/") or f.startswith("reports/"))
            and not f.endswith(".gitkeep")
        }
        self.assertEqual(leaked, set(), f"data/ 与 reports/ 下只允许 .gitkeep,发现:{leaked}")


if __name__ == "__main__":
    unittest.main()
