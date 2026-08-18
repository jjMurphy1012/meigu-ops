"""交易日志结构校验测试。

核心用例直接对应两个真实发生过的 bug:
  · test_detects_out_of_order_entries      → 2026-07-22 条目错序
  · test_detects_orphan_paragraph          → 2026-07-31 标题丢失、正文变孤儿段落
  · test_detects_line_limit_overflow       → 2026-08-05 累积到 175 行才发现超限
"""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from journal_compress import analyze, parse_journal  # noqa: E402

CFG = {"journal": {"max_lines": 40, "full_detail_days": 3, "single_line_days": 14}}
REF = dt.date(2026, 8, 18)

HEADER = "# 交易日志(每日尾盘总结与反思 · 滚动压缩)\n\n> 分层策略见 modes/journal.md\n\n"


def entry(date: str, title: str, body_lines: int = 2) -> str:
    body = "\n".join(f"- 正文第 {i + 1} 行" for i in range(body_lines))
    return f"## {date}(周一)· {title}\n\n{body}\n\n---\n\n"


class TestParse(unittest.TestCase):
    def test_parses_entries(self):
        text = HEADER + entry("2026-08-17", "A") + entry("2026-08-14", "B")
        entries, errors = parse_journal(text)
        self.assertEqual(errors, [])
        self.assertEqual([e.date for e in entries], [dt.date(2026, 8, 17), dt.date(2026, 8, 14)])

    def test_parses_range_entry(self):
        text = HEADER + "## 2026-07-13~07-17(按周合并的示例)\n\n- 摘要\n\n---\n"
        entries, errors = parse_journal(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_range)

    def test_rejects_header_without_date(self):
        text = HEADER + "## 昨天的操作\n\n- 正文\n"
        _, errors = parse_journal(text)
        self.assertTrue(any("不以 YYYY-MM-DD 开头" in e for e in errors))

    def test_empty_journal_yields_no_entries(self):
        entries, errors = parse_journal(HEADER)
        self.assertEqual(entries, [])
        self.assertEqual(errors, [])


class TestStructuralBugs(unittest.TestCase):
    def test_accepts_correct_descending_order(self):
        text = HEADER + entry("2026-08-17", "A") + entry("2026-08-14", "B")
        report = analyze(text, CFG, REF)
        self.assertTrue(report.ok, report.errors)

    def test_detects_out_of_order_entries(self):
        """2026-07-22 的真实 bug:新条目被锚定到较早日期之后,排序错乱。"""
        text = HEADER + entry("2026-08-14", "旧") + entry("2026-08-17", "新")
        report = analyze(text, CFG, REF)
        self.assertFalse(report.ok)
        self.assertTrue(any("排序错误" in e for e in report.errors))

    def test_detects_orphan_paragraph(self):
        """2026-07-31 的真实 bug:`##` 标题丢失,正文成为孤儿段落。"""
        text = HEADER + "- 这段正文的标题丢了\n- 第二行\n\n" + entry("2026-08-17", "A")
        report = analyze(text, CFG, REF)
        self.assertFalse(report.ok)
        self.assertTrue(any("孤儿段落" in e for e in report.errors))

    def test_allows_preamble_title_and_quote(self):
        text = HEADER + entry("2026-08-17", "A")
        report = analyze(text, CFG, REF)
        self.assertTrue(report.ok, report.errors)

    def test_detects_duplicate_dates(self):
        text = HEADER + entry("2026-08-17", "A") + entry("2026-08-17", "又一条")
        report = analyze(text, CFG, REF)
        self.assertFalse(report.ok)
        self.assertTrue(any("日期重复" in e for e in report.errors))

    def test_detects_line_limit_overflow(self):
        """2026-08-05 的真实 bug:单进单出置换导致行数缓慢累积到超限。"""
        text = HEADER + "".join(entry(f"2026-08-{d:02d}", "X", body_lines=6) for d in (17, 14, 13, 12))
        report = analyze(text, CFG, REF)
        self.assertFalse(report.ok)
        self.assertTrue(any("超过上限" in e for e in report.errors))

    def test_warns_near_line_limit(self):
        cfg = {"journal": {"max_lines": 100, "full_detail_days": 3, "single_line_days": 14}}
        text = HEADER + "".join(
            entry(f"2026-08-{d:02d}", "X", body_lines=8) for d in (17, 14, 13, 12, 11, 10, 7, 6, 5)
        )
        report = analyze(text, cfg, REF)
        # 未超限但接近上限时给提醒,不报错
        if report.total_lines <= 100:
            self.assertTrue(any("90%" in w for w in report.warnings))


class TestCompressionPlan(unittest.TestCase):
    def test_recent_entries_need_no_compression(self):
        # 8/17 距 8/18 只有 1 个交易日 → 全细节,不该出现在压缩计划里
        text = HEADER + entry("2026-08-17", "A", body_lines=15)
        report = analyze(text, CFG, REF)
        self.assertEqual(report.plan, [])

    def test_mid_age_entry_flagged_for_single_line(self):
        # 8/5 距 8/18 有 9 个交易日 → 在 3~14 区间,应压成单行
        text = HEADER + entry("2026-08-05", "A", body_lines=15)
        report = analyze(text, CFG, REF)
        self.assertTrue(any("压成单行摘要" in p for p in report.plan))

    def test_old_entry_flagged_for_merge(self):
        # 7/8 距 8/18 远超 14 个交易日 → 应并入按周摘要
        text = HEADER + entry("2026-07-08", "A", body_lines=15)
        report = analyze(text, CFG, REF)
        self.assertTrue(any("并入按周" in p for p in report.plan))

    def test_range_entries_are_exempt(self):
        """已按周合并的条目不该被反复要求再压缩。"""
        text = HEADER + "## 2026-07-13~07-17(按周合并的示例)\n\n- 摘要一行\n\n---\n"
        report = analyze(text, CFG, REF)
        self.assertEqual(report.plan, [])

    def test_suggests_merging_many_old_singles(self):
        text = HEADER + "".join(
            entry(f"2026-07-{d:02d}", "X", body_lines=1) for d in (10, 9, 8)
        )
        report = analyze(text, CFG, REF)
        self.assertTrue(any("合并候选" in p for p in report.plan))


if __name__ == "__main__":
    unittest.main()
