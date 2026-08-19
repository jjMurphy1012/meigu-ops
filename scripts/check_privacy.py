#!/usr/bin/env python3
"""提交前隐私检查 —— 确保用户层数据没有泄进系统层。

用法:
    python3 scripts/check_privacy.py            # 检查全部被 git 跟踪/待提交的文件
    python3 scripts/check_privacy.py --all      # 检查工作区所有文件(忽略 git)

CI 里由 .github/workflows/no-user-data.yml 调用。
这是 AGENTS.md §1 数据分层契约的机器兜底 —— 人会忘,脚本不会。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 与其他 CLI 共用同一套输出编码引导:Windows 控制台编不出 ✅,
# 这个脚本又恰恰是"提交前最后一道防线" —— 它崩了等于没检查。
# 用 try 包住是因为本脚本要能在没有 scripts/ 在 sys.path 时独立运行。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from meigu_lib import _force_utf8_output

    _force_utf8_output()
except Exception:                                 # noqa: BLE001
    pass


ROOT = Path(__file__).resolve().parent.parent

# 绝不应该被提交的路径(即使 .gitignore 漏了)
FORBIDDEN_PATHS = (
    "config/profile.toml",
    "config/watchlist.toml",
    "config/reason-tags.toml",
    "config/rules.toml",
    "modes/_strategy.md",
    ".claude/settings.local.json",
    ".mcp.json",
    ".env",
)
FORBIDDEN_PREFIXES = ("data/", "reports/")

# 内容模式。命中即失败。
PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "brokerage_account_id",
        r"\b\d{9}\b",
        "疑似 9 位券商账户号。账户号只能存在于 config/profile.toml;"
        "文档里指代请写 {{account_id}},展示只露后 4 位。",
    ),
    (
        "absolute_home_path",
        r"/Users/[a-z][a-z0-9._-]+/",
        "绝对家目录路径会泄露用户名。用相对路径或 ~ 代替。",
    ),
    (
        "personal_email",
        r"[a-zA-Z0-9._%+-]+@(?!example\.(com|org)\b)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "疑似真实邮箱地址。",
    ),
    (
        "aws_key",
        r"\bAKIA[0-9A-Z]{16}\b",
        "疑似 AWS access key。",
    ),
    (
        "generic_token",
        r"\b(gh[pousr]|sk-ant-|sk-proj-)[A-Za-z0-9_\-]{16,}",
        "疑似 API token / OAuth token。",
    ),
)

# 逐行豁免标记。含此标记的**单行**跳过内容扫描。
#
# ⚠️ 为什么是逐行而不是整文件白名单:
# 2026-08-18 曾用整文件白名单豁免 tests/test_privacy.py,结果该文件里的一个测试固件
# 用了真实账户号,扫描器被自己的白名单蒙住眼睛,真实账户号被推上公开仓。
# 整文件豁免会连同该文件**未来新增的每一行**一起豁免 —— 这是错误的粒度。
# 逐行标记要求每一处豁免都是显式、当场、可见的决定。
#
# 用法:在需要豁免的那一行末尾加 `# privacy-allow`(或该语言的注释形式)。
PRAGMA = "privacy-allow"

# 明显是占位/示例的值,不算泄露。
# 只列真正的占位形态 —— 绝不要把真实账户号的前缀加进来当例外。
# 只列真正的占位形态 —— 绝不要为了让某个测试通过就放宽它。
# 注意 555000xxx **不在这里**:那是 CI 历史扫描的探针值,内容扫描器必须仍能抓到它,
# 否则 tests/test_privacy.py 里"证明扫描器有效"的用例会被自己白名单掉。
PLACEHOLDER_VALUES = re.compile(r"^(0{9}|1{9}|123456789|987654321|111222333)$")


def tracked_files() -> list[str]:
    """git 跟踪 + 已暂存的文件。"""
    out: set[str] = set()
    quote_off = ["git", "-c", "core.quotepath=false"]
    for cmd in (quote_off + ["ls-files"], quote_off + ["diff", "--cached", "--name-only"]):
        try:
            res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
        out.update(line for line in res.stdout.splitlines() if line.strip())
    return sorted(out)


def _filter_gitignored(paths: list[str]) -> list[str]:
    """剔除被 .gitignore 忽略的文件。

    检查的对象是「会被提交的东西」。已 gitignore 的用户层文件本来就不会进仓,
    扫它们只会产生噪音 —— 真实账户号存在 config/profile.toml 里是设计如此,不是泄漏。
    """
    if not paths:
        return []
    try:
        res = subprocess.run(
            # core.quotepath=false:否则 git 会把中文路径转义成 \347\276\216...,
            # 与我们传入的原始路径比对不上,导致已忽略的文件被误判为待提交。
            ["git", "-c", "core.quotepath=false", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="\n".join(paths),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return paths
    # 退出码 0=有忽略项, 1=无忽略项, 128=不是 git 仓库
    if res.returncode not in (0, 1):
        return paths
    ignored = {line.strip() for line in res.stdout.splitlines() if line.strip()}
    return [p for p in paths if p not in ignored]


def all_files() -> list[str]:
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules"}
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        files.append(str(rel))
    return _filter_gitignored(sorted(files))


def is_text(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def check(files: list[str]) -> list[str]:
    problems: list[str] = []

    for rel in files:
        if rel in FORBIDDEN_PATHS:
            problems.append(f"{rel}: 用户层文件被纳入版本控制 —— 必须 git rm --cached 并确认 .gitignore")
        if any(rel.startswith(pre) for pre in FORBIDDEN_PREFIXES) and not rel.endswith(".gitkeep"):
            problems.append(f"{rel}: {rel.split('/')[0]}/ 下只允许提交 .gitkeep")

    for rel in files:
        path = ROOT / rel
        if not path.exists() or not is_text(path):
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if PRAGMA in line:
                continue
            for name, pattern, hint in PATTERNS:
                for m in re.finditer(pattern, line):
                    value = m.group(0)
                    if name == "brokerage_account_id" and PLACEHOLDER_VALUES.match(value):
                        continue
                    if name == "personal_email" and (
                        "noreply" in value or value.endswith(".example")
                    ):
                        continue
                    problems.append(f"{rel}:{line_no}: [{name}] {value!r} —— {hint}")

    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提交前隐私检查")
    ap.add_argument("--all", action="store_true", help="检查工作区所有文件而非只查 git 跟踪的")
    args = ap.parse_args(argv)

    files = all_files() if args.all else (tracked_files() or all_files())
    problems = check(files)

    print(f"=== 隐私检查 · {len(files)} 个文件 ===")
    if not problems:
        print("✅ 未发现用户层数据泄漏。")
        return 0

    print(f"\n❌ {len(problems)} 个问题:\n")
    for p in problems:
        print(f"  · {p}")
    print(
        "\n数据分层契约见 AGENTS.md §1 / docs/DATA_CONTRACT.md。"
        "\n判断标准:这是「怎么做」(系统层,可提交)还是「做了什么」(用户层,进 data/)?"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
