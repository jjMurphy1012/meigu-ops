"""台账解析与 FIFO 统计测试。

台账缺一笔或格式错一处,整段历史的 FIFO 配对都会错 —— 所以校验必须严格,
而且必须报出行号。这些测试锁住那个行为。
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from meigu_lib import TRADE_COLUMNS, LedgerError, Vocabulary, parse_trades  # noqa: E402
from stats import fifo_match, summarize  # noqa: E402

HEADER = "\t".join(TRADE_COLUMNS)

# ★ 测试自带词表 —— 绝不依赖 config/reason-tags.toml。
# 否则本机的私有配置一改,测试就红/绿漂移,而且换个人 clone 结果不同。
TEST_VOCAB = Vocabulary(buy=("建仓", "加仓"), sell=("减仓", "清仓"))



def tsv(*rows: str) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8")
    fh.write(HEADER + "\n")
    for r in rows:
        fh.write(r + "\n")
    fh.close()
    return Path(fh.name)


def row(date, cp, sym, side, qty, price, amount, tag, pct="", note=""):
    return "\t".join([date, cp, sym, side, str(qty), str(price), str(amount), tag, str(pct), note])


class TestParse(unittest.TestCase):
    def test_parses_valid_rows(self):
        p = tsv(
            row("2026-08-06", "10:33", "DDDD", "buy", 0.5, 100.0, 50.0, "建仓", 100, "重建仓"),
            row("2026-08-10", "13:03", "DDDD", "sell", 0.25, 120.0, 30.0, "减仓", 40),
        )
        trades = parse_trades(p, vocab=TEST_VOCAB)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].symbol, "DDDD")
        self.assertEqual(trades[0].side, "buy")
        self.assertEqual(trades[1].reason_tag, "减仓")

    def test_skips_comments_and_blank_lines(self):
        p = tsv(
            "# 这是注释",
            "",
            row("2026-08-06", "10:33", "AAAA", "buy", 1, 300.0, 300.0, "建仓"),
        )
        self.assertEqual(len(parse_trades(p, vocab=TEST_VOCAB)), 1)

    def test_missing_file_returns_empty(self):
        self.assertEqual(parse_trades(Path("/nonexistent/trades.tsv"), vocab=TEST_VOCAB), [])

    def test_normalizes_symbol_case_and_currency(self):
        p = tsv(row("2026-08-06", "10:33", "aaaa", "BUY", 1, "$300.50", "$300.50", "建仓"))
        t = parse_trades(p, vocab=TEST_VOCAB)[0]
        self.assertEqual(t.symbol, "AAAA")
        self.assertEqual(t.side, "buy")
        self.assertAlmostEqual(t.price, 300.50)

    def test_sorts_by_date(self):
        p = tsv(
            row("2026-08-10", "10:33", "CCCC", "buy", 1, 200.0, 200.0, "建仓"),
            row("2026-08-06", "10:33", "CCCC", "buy", 1, 100.0, 100.0, "建仓"),
        )
        trades = parse_trades(p, vocab=TEST_VOCAB)
        self.assertEqual(trades[0].date, dt.date(2026, 8, 6))


class TestValidation(unittest.TestCase):
    def test_rejects_wrong_field_count(self):
        p = tsv("2026-08-06\t10:33\tAAAA\tbuy")
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("制表符分隔字段", str(ctx.exception))

    def test_rejects_bad_date(self):
        p = tsv(row("08/06/2026", "10:33", "AAAA", "buy", 1, 300.0, 300.0, "建仓"))
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("日期格式", str(ctx.exception))

    def test_rejects_bad_side(self):
        p = tsv(row("2026-08-06", "10:33", "AAAA", "hold", 1, 300.0, 300.0, "建仓"))
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("side", str(ctx.exception))

    def test_rejects_unknown_reason_tag(self):
        p = tsv(row("2026-08-06", "10:33", "AAAA", "buy", 1, 300.0, 300.0, "随便买买"))
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("不在词表内", str(ctx.exception))

    def test_rejects_sell_tag_on_buy_row(self):
        """买入行用卖出标签是常见笔误,必须拦住。"""
        p = tsv(row("2026-08-06", "10:33", "AAAA", "buy", 1, 300.0, 300.0, "减仓"))
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("不应使用标签", str(ctx.exception))

    def test_rejects_unparseable_number(self):
        p = tsv(row("2026-08-06", "10:33", "AAAA", "buy", "一股", 300.0, 300.0, "建仓"))
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("无法解析为数字", str(ctx.exception))

    def test_error_reports_line_number(self):
        p = tsv(
            row("2026-08-06", "10:33", "AAAA", "buy", 1, 300.0, 300.0, "建仓"),
            row("2026-08-07", "10:33", "AAAA", "buy", 1, 300.0, 300.0, "瞎买"),
        )
        with self.assertRaises(LedgerError) as ctx:
            parse_trades(p, vocab=TEST_VOCAB)
        self.assertIn("第 3 行", str(ctx.exception))


class TestFifo(unittest.TestCase):
    def test_simple_profit(self):
        p = tsv(
            row("2026-08-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-08-11", "13:03", "X", "sell", 1, 110.0, 110.0, "减仓"),
        )
        matches, warnings, open_qty = fifo_match(parse_trades(p, vocab=TEST_VOCAB))
        self.assertEqual(warnings, [])
        self.assertEqual(len(matches), 1)
        self.assertAlmostEqual(matches[0].pnl, 10.0)
        self.assertEqual(matches[0].holding_days, 10)
        self.assertEqual(open_qty, {})

    def test_fifo_order_matters(self):
        """先买的先出:两笔不同成本的买入,卖一半应配对到较早的那笔。"""
        p = tsv(
            row("2026-08-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-08-05", "10:33", "X", "buy", 1, 200.0, 200.0, "加仓"),
            row("2026-08-11", "13:03", "X", "sell", 1, 150.0, 150.0, "减仓"),
        )
        matches, _, open_qty = fifo_match(parse_trades(p, vocab=TEST_VOCAB))
        self.assertEqual(len(matches), 1)
        self.assertAlmostEqual(matches[0].buy_price, 100.0)
        self.assertAlmostEqual(matches[0].pnl, 50.0)
        self.assertAlmostEqual(open_qty["X"], 1.0)

    def test_sell_spanning_multiple_lots(self):
        p = tsv(
            row("2026-08-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-08-05", "10:33", "X", "buy", 1, 200.0, 200.0, "加仓"),
            row("2026-08-11", "13:03", "X", "sell", 2, 150.0, 300.0, "清仓"),
        )
        matches, _, open_qty = fifo_match(parse_trades(p, vocab=TEST_VOCAB))
        self.assertEqual(len(matches), 2)
        self.assertAlmostEqual(sum(m.pnl for m in matches), 0.0)
        self.assertEqual(open_qty, {})

    def test_fractional_shares(self):
        p = tsv(
            row("2026-08-01", "10:33", "X", "buy", 0.13245, 377.50, 50.0, "建仓"),
            row("2026-08-11", "13:03", "X", "sell", 0.07947, 390.00, 31.0, "减仓"),
        )
        matches, warnings, open_qty = fifo_match(parse_trades(p, vocab=TEST_VOCAB))
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(matches[0].pnl, 0.07947 * 12.5, places=5)
        self.assertAlmostEqual(open_qty["X"], 0.05298, places=5)

    def test_warns_on_sell_without_buy(self):
        """台账漏记买入时必须警告,而不是静默算错。"""
        p = tsv(row("2026-08-11", "13:03", "X", "sell", 1, 150.0, 150.0, "减仓"))
        matches, warnings, _ = fifo_match(parse_trades(p, vocab=TEST_VOCAB))
        self.assertEqual(matches, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("没有对应买入记录", warnings[0])

    def test_symbols_are_independent(self):
        p = tsv(
            row("2026-08-01", "10:33", "A", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-08-01", "10:33", "B", "buy", 1, 50.0, 50.0, "建仓"),
            row("2026-08-11", "13:03", "A", "sell", 1, 110.0, 110.0, "减仓"),
        )
        matches, warnings, open_qty = fifo_match(parse_trades(p, vocab=TEST_VOCAB))
        self.assertEqual(warnings, [])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].symbol, "A")
        self.assertAlmostEqual(open_qty["B"], 1.0)


class TestSummarize(unittest.TestCase):
    def test_empty_ledger(self):
        s = summarize([], vocab=TEST_VOCAB)
        self.assertEqual(s["trade_count"], 0)
        self.assertIsNone(s["win_rate"])
        self.assertFalse(s["sample_sufficient"])

    def test_tag_and_checkpoint_aggregation(self):
        p = tsv(
            row("2026-08-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-08-11", "15:37", "X", "sell", 1, 110.0, 110.0, "减仓"),
        )
        s = summarize(parse_trades(p, vocab=TEST_VOCAB), vocab=TEST_VOCAB)
        self.assertEqual(s["buy_count"], 1)
        self.assertEqual(s["sell_count"], 1)
        self.assertAlmostEqual(s["realized_pnl"], 10.0)
        self.assertEqual(s["win_rate"], 100.0)
        self.assertIsNone(s["by_tag"]["建仓"]["pnl"])  # 买入标签不归集盈亏
        self.assertAlmostEqual(s["by_tag"]["减仓"]["pnl"], 10.0)
        self.assertEqual(s["by_checkpoint"]["10:33"]["buy"], 1)
        self.assertAlmostEqual(s["by_checkpoint"]["15:37"]["pnl"], 10.0)

    def test_sample_sufficiency_flag(self):
        rows = []
        for i in range(1, 12):
            rows.append(row(f"2026-08-{i:02d}", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"))
            rows.append(row(f"2026-09-{i:02d}", "13:03", "X", "sell", 1, 110.0, 110.0, "减仓"))
        s = summarize(parse_trades(tsv(*rows), vocab=TEST_VOCAB), vocab=TEST_VOCAB)
        self.assertEqual(s["closed_pairs"], 11)
        self.assertFalse(s["sample_sufficient"])  # 11 < MIN_SAMPLE(20)

    def test_monthly_breakdown(self):
        p = tsv(
            row("2026-07-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-08-11", "13:03", "X", "sell", 1, 110.0, 110.0, "减仓"),
        )
        s = summarize(parse_trades(p, vocab=TEST_VOCAB), vocab=TEST_VOCAB)
        self.assertIn("2026-07", s["by_month"])
        self.assertAlmostEqual(s["by_month"]["2026-08"]["pnl"], 10.0)


if __name__ == "__main__":
    unittest.main()


class TestOptionalRuleIdsColumn(unittest.TestCase):
    """第 11 列 rule_ids 是后加的 —— 旧的 10 列台账必须继续可读。"""

    def test_ten_column_row_still_parses(self):
        p = tsv(row("2026-08-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"))
        t = parse_trades(p, vocab=TEST_VOCAB)[0]
        self.assertEqual(t.rule_ids, [])

    def test_eleven_column_row_parses_rule_ids(self):
        line = "\t".join(["2026-08-01", "10:33", "X", "buy", "1", "100.0", "100.0",
                          "建仓", "", "备注", "rule-a;rule-b"])
        t = parse_trades(tsv(line), vocab=TEST_VOCAB)[0]
        self.assertEqual(t.rule_ids, ["rule-a", "rule-b"])

    def test_too_few_columns_still_rejected(self):
        with self.assertRaises(LedgerError):
            parse_trades(tsv("2026-08-01\t10:33\tX\tbuy"), vocab=TEST_VOCAB)
