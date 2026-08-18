#!/usr/bin/env python3
"""交易日志的结构校验与分层压缩规划。

用法:
    python3 scripts/journal_compress.py --check    # 只校验结构(CI / 写入前跑)
    python3 scripts/journal_compress.py            # 校验 + 输出压缩计划
    python3 scripts/journal_compress.py --json

这个脚本的存在理由是两个真实发生过的 bug:

  1. 2026-07-22 —— 追加条目时误锚定较早日期,导致条目错序,数日后才发现。
  2. 2026-07-31 —— 编辑中丢失一个 `##` 标题,正文变成挂在别人下面的孤儿段落。

`Edit` 工具返回成功只代表字符串替换执行了,不代表文档结构是对的。
所以这层校验必须由脚本兜底,不能依赖人眼。

摘要文字仍由人/LLM 写 —— 脚本只负责"哪些条目该压、压到几行",不臆造语义。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field

from meigu_lib import JOURNAL_MD, load_config, today_et, trading_days_between

# `## 2026-08-17(周一)· 标题` 或 `## 2026-07-13~07-17(按周合并的示例)`
ENTRY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s*[~～]\s*(\d{2}-\d{2}|\d{4}-\d{2}-\d{2}))?")

# 压缩后的分级行数上限
TIER_SINGLE_LINE_MAX = 8   # 3 日 ~ 2 周:压成"单行摘要"(留标题+分隔符+少量行的余量)
TIER_MERGED_MAX = 4        # >2 周:应已并入按周/按轮摘要


@dataclass
class Entry:
    start_line: int
    end_line: int
    date: dt.date
    is_range: bool
    title: str
    body_lines: list[str] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class Report:
    total_lines: int
    max_lines: int
    entries: list[Entry]
    errors: list[str]
    warnings: list[str]
    plan: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_journal(text: str) -> tuple[list[Entry], list[str]]:
    lines = text.splitlines()
    entries: list[Entry] = []
    errors: list[str] = []
    preamble_content: list[int] = []

    starts: list[tuple[int, re.Match]] = []
    for idx, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m:
            starts.append((idx, m))
        elif line.startswith("## "):
            errors.append(
                f"第 {idx + 1} 行:`## ` 标题不以 YYYY-MM-DD 开头 —— {line[:60]!r}。"
                f"条目标题格式必须是 `## YYYY-MM-DD(周X)· 标题`。"
            )

    if not starts:
        return [], errors

    # 首个条目之前只允许:文件标题(`# `)、引文(`> `)、注释、水平分隔线、空行。
    # 其他任何正文出现在这里,都说明它原本的 `## ` 标题丢了。
    for idx in range(starts[0][0]):
        line = lines[idx].strip()
        if not line or set(line) <= {"-"} or set(line) <= {"="}:
            continue
        if line.startswith("# ") or line.startswith(">") or line.startswith("<!--"):
            continue
        preamble_content.append(idx + 1)
    if preamble_content:
        errors.append(
            f"第 {preamble_content[0]} 行起有正文出现在第一个条目标题之前 —— "
            f"这是「孤儿段落」,说明某个 `##` 标题丢了(2026-07-31 踩过)。"
        )

    for i, (idx, m) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines) - 1
        date_s, range_end = m.group(1), m.group(2)
        entries.append(
            Entry(
                start_line=idx + 1,
                end_line=end + 1,
                date=dt.date.fromisoformat(date_s),
                is_range=range_end is not None,
                title=lines[idx].lstrip("# ").strip(),
                body_lines=lines[idx + 1 : end + 1],
            )
        )
    return entries, errors


def analyze(text: str, cfg: dict, ref_date: dt.date | None = None) -> Report:
    journal_cfg = cfg.get("journal", {})
    max_lines = int(journal_cfg.get("max_lines", 150))
    full_detail_days = int(journal_cfg.get("full_detail_days", 3))
    single_line_days = int(journal_cfg.get("single_line_days", 14))
    ref = ref_date or today_et()

    entries, errors = parse_journal(text)
    warnings: list[str] = []
    plan: list[str] = []
    total_lines = len(text.splitlines())

    # --- 排序校验(2026-07-22 踩过)
    for a, b in zip(entries, entries[1:]):
        if b.date >= a.date:
            errors.append(
                f"排序错误:第 {b.start_line} 行的 {b.date} 出现在第 {a.start_line} 行的 {a.date} 之后。"
                f"条目必须严格按日期倒序,最新的在最上面。"
            )

    # --- 重复日期
    seen: dict[dt.date, int] = {}
    for e in entries:
        if e.date in seen and not e.is_range:
            errors.append(f"日期重复:{e.date} 同时出现在第 {seen[e.date]} 行和第 {e.start_line} 行。")
        seen.setdefault(e.date, e.start_line)

    # --- 行数上限
    if total_lines > max_lines:
        errors.append(
            f"全文 {total_lines} 行,超过上限 {max_lines} 行。"
            f"按 modes/journal.md Step 4 一次性把 >2 周的多条合并成按周摘要 —— "
            f"不要只压最老的一条(2026-08-05 就是这么累积到超限的)。"
        )
    elif total_lines > max_lines * 0.9:
        warnings.append(f"全文 {total_lines} 行,已达上限 {max_lines} 行的 90%,下次写入前先压缩。")

    # --- 分层合规
    for e in entries:
        age = trading_days_between(e.date, ref)
        if e.is_range:
            continue
        if age <= full_detail_days:
            continue
        if age <= single_line_days:
            if e.line_count > TIER_SINGLE_LINE_MAX:
                plan.append(
                    f"压成单行摘要:{e.date}(第 {e.start_line}-{e.end_line} 行,"
                    f"{e.line_count} 行 → ≤{TIER_SINGLE_LINE_MAX} 行)· 距今 {age} 交易日"
                )
        else:
            if e.line_count > TIER_MERGED_MAX:
                plan.append(
                    f"并入按周/按轮摘要:{e.date}(第 {e.start_line}-{e.end_line} 行,"
                    f"{e.line_count} 行)· 距今 {age} 交易日,已超 2 周"
                )

    # --- 超过 2 周的独立单日条目建议合并
    old_singles = [
        e for e in entries if not e.is_range and trading_days_between(e.date, ref) > single_line_days
    ]
    if len(old_singles) >= 3:
        span = f"{old_singles[-1].date} ~ {old_singles[0].date}"
        plan.append(
            f"合并候选:{len(old_singles)} 条超 2 周的独立单日条目({span})可按周合并成数行。"
        )

    return Report(total_lines, max_lines, entries, errors, warnings, plan)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="交易日志结构校验与压缩规划")
    ap.add_argument("--check", action="store_true", help="只校验结构,不输出压缩计划")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--file", help="指定日志文件路径(默认 data/journal.md)")
    ap.add_argument("--ref-date", help="以该日期为「今天」计算条目年龄(测试用)")
    args = ap.parse_args(argv)

    path = JOURNAL_MD if not args.file else __import__("pathlib").Path(args.file)
    if not path.exists():
        print(f"⚠️  {path} 不存在 —— 还没有交易日志,跳过。")
        return 0

    cfg = load_config("profile", required=False)
    ref = dt.date.fromisoformat(args.ref_date) if args.ref_date else None
    report = analyze(path.read_text(encoding="utf-8"), cfg, ref)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "total_lines": report.total_lines,
                    "max_lines": report.max_lines,
                    "entry_count": len(report.entries),
                    "errors": report.errors,
                    "warnings": report.warnings,
                    "plan": [] if args.check else report.plan,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report.ok else 1

    print(f"=== 交易日志结构校验 · {path.name} ===")
    print(f"条目数:{len(report.entries)} · 全文 {report.total_lines}/{report.max_lines} 行")
    if report.entries:
        print(f"最新条目:{report.entries[0].date} · 最早条目:{report.entries[-1].date}")

    if report.errors:
        print(f"\n❌ {len(report.errors)} 个结构错误(必须修):")
        for e in report.errors:
            print(f"  · {e}")
    else:
        print("\n✅ 结构完好:标题齐全、严格倒序、无孤儿段落、未超行数上限。")

    if report.warnings:
        print("\n⚠️  提醒:")
        for w in report.warnings:
            print(f"  · {w}")

    if not args.check:
        if report.plan:
            print(f"\n📋 压缩计划({len(report.plan)} 项,摘要文字由你写):")
            for p in report.plan:
                print(f"  · {p}")
            print("\n   分层规则见 modes/journal.md Step 4。")
            print("   教训类内容已进 modes/*.md 的,日志里只留一句指针,不重复叙述。")
        else:
            print("\n📋 无需压缩:所有条目都符合分层规则。")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
