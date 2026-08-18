"""dashboard 渲染测试。

整个 UI 是纯函数 `render_*(data, width, height, state) -> list[str]`,
curses 只负责贴字符 —— 所以界面可以被完整单元测试。

最重要的一条不变量:**每一行的显示宽度必须精确等于 width**。
中文是双宽字符,一旦算错,整个表格会错位,而错位的表格会被人当成数据读错。
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as dash  # noqa: E402
from dashboard import (  # noqa: E402
    TABS,
    DashboardData,
    Position,
    Snapshot,
    UiState,
    bar,
    core_rule_verdicts,
    discipline_rows,
    dwidth,
    load_dashboard_data,
    money,
    pad,
    parse_snapshot,
    pct,
    render_screen,
    sort_trades,
    trade_pnl,
)
from meigu_lib import TRADE_COLUMNS, Vocabulary, parse_trades  # noqa: E402
from stats import fifo_match, summarize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 宽度与格式
class TestWidth(unittest.TestCase):
    def test_ascii_width(self):
        self.assertEqual(dwidth("AAAA"), 4)

    def test_cjk_is_double_width(self):
        self.assertEqual(dwidth("宽度测试"), 8)

    def test_mixed_width(self):
        self.assertEqual(dwidth("AAAA 宽度测试"), 4 + 1 + 8)

    def test_ambiguous_chars_counted_as_narrow(self):
        """记录一个明确的假设:歧义宽度字符(… ▓ ░)按 1 列算。

        绝大多数现代终端与 vhs 的渲染器都按 1 列渲染它们。
        若将来发现 gif 或某终端里表格错位,先回来改这条假设。
        """
        for ch in "…▓░":
            self.assertEqual(dwidth(ch), 1, ch)

    def test_pad_fills_to_exact_width(self):
        self.assertEqual(dwidth(pad("AAAA", 10)), 10)
        self.assertEqual(dwidth(pad("宽度测试", 12)), 12)

    def test_pad_right_align(self):
        self.assertEqual(pad("42", 5, ">"), "   42")

    def test_pad_center(self):
        self.assertEqual(pad("ab", 6, "^"), "  ab  ")

    def test_pad_truncates_too_long(self):
        out = pad("这是一个很长的中文字符串", 10)
        self.assertEqual(dwidth(out), 10)          # 精确,不是"不超过"
        self.assertTrue(out.rstrip().endswith("…"))  # 截断后可能补了空格

    def test_pad_zero_width(self):
        self.assertEqual(pad("abc", 0), "")
        self.assertEqual(pad("abc", -3), "")

    def test_pad_truncation_is_exact_width_even_with_cjk(self):
        """截断双宽字符时必须补齐 —— 少 1 列就会让整张表错位。"""
        for n in range(1, 20):
            for s in ("减仓减仓", "AAAA 减仓 x", "▓▓▓░░░ 48.6%超上限"):
                self.assertEqual(dwidth(pad(s, n)), n, f"{s!r} @ {n}")


class TestFormat(unittest.TestCase):
    def test_money(self):
        self.assertEqual(money(1234.5), "$1,234.50")
        self.assertEqual(money(None), "—")

    def test_money_signed(self):
        self.assertEqual(money(5.0, signed=True), "+$5.00")
        self.assertEqual(money(-5.0, signed=True), "-$5.00")

    def test_pct(self):
        self.assertEqual(pct(7.5), "+7.5%")
        self.assertEqual(pct(-3.0), "-3.0%")
        self.assertEqual(pct(None), "—")
        self.assertEqual(pct(0.0), "+0.0%")


class TestBar(unittest.TestCase):
    def test_empty_and_full(self):
        self.assertEqual(bar(0, 5), "░░░░░")
        self.assertEqual(bar(100, 5), "▓▓▓▓▓")

    def test_half(self):
        self.assertEqual(bar(50, 10), "▓▓▓▓▓░░░░░")

    def test_width_is_exact(self):
        for p in (0, 13, 47, 99, 100):
            self.assertEqual(len(bar(p, 9)), 9)

    def test_cap_marker_when_over_limit(self):
        self.assertTrue(bar(60, 9, cap_pct=50).endswith("!"))
        self.assertFalse(bar(40, 9, cap_pct=50).endswith("!"))

    def test_clamps_above_100(self):
        self.assertEqual(len(bar(180, 6)), 6)


# ---------------------------------------------------------------------- 快照
SNAP = {
    "date": "2026-08-18",
    "checkpoints": {
        "10:33": {"captured_at_et": "2026-08-18 10:33:00",
                  "normalized": {"total_value": 100.0, "positions": []}},
        "15:37": {
            "captured_at_et": "2026-08-18 15:37:00",
            "normalized": {
                "total_value": 500.0, "cash": 90.0, "buying_power": 90.0,
                "equity_value": 410.0,
                "positions": [
                    {"symbol": "cccc", "qty": 1.0, "avg_cost": 100.0,
                     "price": 90.0, "market_value": 90.0},
                    {"symbol": "AAAA", "qty": 0.5, "avg_cost": 400.0,
                     "price": 440.0, "market_value": 220.0},
                ],
            },
        },
    },
}


class TestParseSnapshot(unittest.TestCase):
    def test_picks_latest_checkpoint(self):
        s = parse_snapshot(SNAP, "x.json")
        self.assertEqual(s.checkpoint, "15:37")
        self.assertEqual(s.total_value, 500.0)

    def test_positions_sorted_by_market_value_desc(self):
        s = parse_snapshot(SNAP, "x.json")
        self.assertEqual([p.symbol for p in s.positions], ["AAAA", "CCCC"])

    def test_symbol_uppercased(self):
        s = parse_snapshot(SNAP, "x.json")
        self.assertIn("CCCC", [p.symbol for p in s.positions])

    def test_missing_normalized_produces_note_not_crash(self):
        s = parse_snapshot({"date": "d", "checkpoints": {"10:33": {}}}, "x.json")
        self.assertEqual(s.positions, [])
        self.assertIn("normalized", s.note)

    def test_no_checkpoints(self):
        s = parse_snapshot({"date": "d"}, "x.json")
        self.assertEqual(s.checkpoint, "")
        self.assertIn("normalized", s.note)

    def test_bad_position_row_is_skipped_with_note(self):
        payload = {
            "checkpoints": {"10:33": {"normalized": {
                "positions": [{"symbol": "X", "qty": "abc"}]}}}
        }
        s = parse_snapshot(payload, "x.json")
        self.assertEqual(s.positions, [])
        self.assertIn("无法解析", s.note)

    def test_example_fixture_parses(self):
        payload = json.loads(
            (ROOT / "examples" / "sample-snapshot.json").read_text(encoding="utf-8"))
        s = parse_snapshot(payload, "examples/sample-snapshot.json")
        self.assertEqual(len(s.positions), 3)
        self.assertEqual(s.checkpoint, "15:37")


class TestSnapshotMath(unittest.TestCase):
    def setUp(self):
        self.s = parse_snapshot(SNAP, "x.json")

    def test_bp_pct(self):
        self.assertAlmostEqual(self.s.bp_pct, 18.0)

    def test_bp_pct_none_without_total(self):
        self.assertIsNone(Snapshot(buying_power=10.0).bp_pct)

    def test_share_pct(self):
        avgo = self.s.positions[0]
        self.assertAlmostEqual(self.s.share_pct(avgo), 220.0 / 410.0 * 100)

    def test_position_pnl(self):
        avgo = self.s.positions[0]
        self.assertAlmostEqual(avgo.pnl_pct, 10.0)
        self.assertAlmostEqual(avgo.pnl_usd, 20.0)

    def test_position_pnl_pct_none_without_cost(self):
        self.assertIsNone(Position("X", 1, 0, 5, 5).pnl_pct)

    def test_total_pnl(self):
        # AAAA +$20, CCCC -$10
        self.assertAlmostEqual(self.s.total_pnl_usd, 10.0)


# ---------------------------------------------------------------------- 台账
HEADER = "\t".join(TRADE_COLUMNS)

# ★ 测试自带词表 —— 绝不依赖 config/reason-tags.toml。
# 否则本机的私有配置一改,测试就红/绿漂移,而且换个人 clone 结果不同。
TEST_VOCAB = Vocabulary(buy=("建仓", "加仓"), sell=("减仓", "清仓"))



def make_ledger(*rows: str) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8")
    fh.write(HEADER + "\n")
    for r in rows:
        fh.write(r + "\n")
    fh.close()
    return Path(fh.name)


def row(date, cp, sym, side, qty, price, amount, tag, pctpos="", note=""):
    return "\t".join([date, cp, sym, side, str(qty), str(price),
                      str(amount), tag, str(pctpos), note])


class TestSortTrades(unittest.TestCase):
    def setUp(self):
        p = make_ledger(
            row("2026-06-01", "10:33", "AAAA", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-07-01", "13:03", "ZZZZ", "buy", 1, 50.0, 50.0, "建仓"),
            row("2026-06-15", "15:37", "MMMM", "sell", 1, 120.0, 20.0, "减仓"),
        )
        self.trades = parse_trades(p, vocab=TEST_VOCAB)

    def test_sort_by_date_desc(self):
        out = sort_trades(self.trades, 0, True)
        self.assertEqual(out[0].date, dt.date(2026, 7, 1))

    def test_sort_by_date_asc(self):
        out = sort_trades(self.trades, 0, False)
        self.assertEqual(out[0].date, dt.date(2026, 6, 1))

    def test_sort_by_symbol(self):
        out = sort_trades(self.trades, 1, False)
        self.assertEqual([t.symbol for t in out], ["AAAA", "MMMM", "ZZZZ"])

    def test_sort_by_amount_desc(self):
        out = sort_trades(self.trades, 2, True)
        self.assertEqual(out[0].amount, 100.0)

    def test_sort_by_tag(self):
        out = sort_trades(self.trades, 3, False)
        self.assertEqual([t.reason_tag for t in out], sorted(t.reason_tag for t in out))

    def test_sort_is_stable_and_total(self):
        for idx in range(len(dash.SORT_KEYS)):
            for desc in (True, False):
                self.assertEqual(len(sort_trades(self.trades, idx, desc)), 3)


class TestTradePnl(unittest.TestCase):
    def setUp(self):
        p = make_ledger(
            row("2026-06-01", "10:33", "X", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-06-15", "13:03", "X", "sell", 1, 110.0, 110.0, "减仓"),
        )
        self.trades = parse_trades(p, vocab=TEST_VOCAB)
        self.matches, _, _ = fifo_match(self.trades)

    def test_buy_row_has_no_pnl(self):
        buy = next(t for t in self.trades if t.side == "buy")
        self.assertIsNone(trade_pnl(buy, self.matches))

    def test_sell_row_pnl(self):
        sell = next(t for t in self.trades if t.side == "sell")
        self.assertAlmostEqual(trade_pnl(sell, self.matches), 10.0)

    def test_unmatched_sell_returns_none(self):
        p = make_ledger(row("2026-06-15", "13:03", "Y", "sell", 1, 110.0, 110.0, "减仓"))
        trades = parse_trades(p, vocab=TEST_VOCAB)
        matches, _, _ = fifo_match(trades)
        self.assertIsNone(trade_pnl(trades[0], matches))


# ------------------------------------------------------------------ 纪律判定
def summary_from(*rows: str) -> dict:
    return summarize(parse_trades(make_ledger(*rows), vocab=TEST_VOCAB), vocab=TEST_VOCAB)


class TestDisciplineRows(unittest.TestCase):
    """标签绩效表 —— 规则判定本身的测试在 tests/test_rules.py。"""

    def test_discipline_rows_cover_all_used_tags(self):
        s = summary_from(
            row("2026-06-01", "10:33", "A", "buy", 1, 100.0, 100.0, "建仓"),
            row("2026-06-10", "13:03", "A", "sell", 1, 120.0, 120.0, "减仓"),
        )
        tags = {r[0] for r in discipline_rows(s)}
        self.assertEqual(tags, {"建仓", "减仓"})

    def test_empty_ledger_has_no_rows(self):
        self.assertEqual(discipline_rows(summarize([], vocab=TEST_VOCAB)), [])

    def test_buy_rows_have_no_pnl(self):
        s = summary_from(row("2026-06-01", "10:33", "A", "buy", 1, 100.0, 100.0, "建仓"))
        row_ = next(r for r in discipline_rows(s) if r[0] == "建仓")
        self.assertIsNone(row_[4])


# ------------------------------------------------------------------ 整屏渲染
def demo_data() -> DashboardData:
    return load_dashboard_data(demo=True)


def empty_data() -> DashboardData:
    return DashboardData(
        snapshot=Snapshot(note="还没有任何快照。"),
        trades=[], matches=[], summary=summarize([], vocab=TEST_VOCAB),
        cfg={"cash": {"bp_target_pct": 20}, "position": {"max_single_pct": 50}},
        demo=False,
    )


class TestRenderScreen(unittest.TestCase):
    """核心不变量:每行宽度精确等于 width,行数精确等于 height。"""

    def _assert_grid(self, lines, width, height):
        self.assertEqual(len(lines), height, f"行数 {len(lines)} != {height}")
        for i, line in enumerate(lines):
            self.assertEqual(
                dwidth(line), width,
                f"第 {i} 行宽度 {dwidth(line)} != {width}: {line!r}",
            )

    def test_every_tab_renders_exact_grid(self):
        data = demo_data()
        for tab in range(len(TABS)):
            for width, height in ((80, 24), (100, 30), (140, 40)):
                with self.subTest(tab=TABS[tab], size=(width, height)):
                    lines = render_screen(data, width, height, UiState(tab=tab))
                    self._assert_grid(lines, width, height)

    def test_empty_data_renders_without_crash(self):
        data = empty_data()
        for tab in range(len(TABS)):
            lines = render_screen(data, 90, 26, UiState(tab=tab))
            self._assert_grid(lines, 90, 26)

    def test_detail_overlay_renders(self):
        data = demo_data()
        lines = render_screen(data, 100, 30, UiState(tab=1, cursor=2, detail=True))
        self._assert_grid(lines, 100, 30)
        self.assertTrue(any("FIFO 配对" in ln for ln in lines))

    def test_help_overlay_renders(self):
        lines = render_screen(demo_data(), 100, 30, UiState(help=True))
        self._assert_grid(lines, 100, 30)
        self.assertTrue(any("不构成投资建议" in ln for ln in lines))

    def test_active_tab_is_marked(self):
        for tab in range(len(TABS)):
            lines = render_screen(demo_data(), 100, 24, UiState(tab=tab))
            self.assertIn(f"[{tab + 1} {TABS[tab]}]", lines[0])

    def test_demo_badge_shown_only_in_demo(self):
        self.assertIn("DEMO", render_screen(demo_data(), 100, 24, UiState())[0])
        self.assertNotIn("DEMO", render_screen(empty_data(), 100, 24, UiState())[0])

    def test_cursor_marker_present(self):
        lines = render_screen(demo_data(), 100, 30, UiState(tab=1, cursor=0))
        self.assertTrue(any(ln.startswith("▸") for ln in lines))

    def test_tiny_terminal_does_not_crash(self):
        lines = render_screen(demo_data(), 40, 12, UiState(tab=1))
        self._assert_grid(lines, 40, 12)

    def test_portfolio_shows_bp_target(self):
        lines = render_screen(demo_data(), 100, 26, UiState(tab=0))
        self.assertTrue(any("目标<" in ln for ln in lines))

    def test_discipline_shows_core_rule_section(self):
        lines = render_screen(demo_data(), 100, 34, UiState(tab=2))
        self.assertTrue(any("你的规则是否被数据支持" in ln for ln in lines))

    def test_ledger_warns_on_small_sample(self):
        lines = render_screen(demo_data(), 110, 30, UiState(tab=1))
        self.assertTrue(any("样本不足" in ln for ln in lines))


class TestUiState(unittest.TestCase):
    def test_clamp_within_bounds(self):
        data = demo_data()
        st = UiState(tab=1, cursor=999)
        st.clamp(data)
        self.assertEqual(st.cursor, len(data.trades) - 1)

    def test_clamp_negative(self):
        data = demo_data()
        st = UiState(tab=0, cursor=-5)
        st.clamp(data)
        self.assertEqual(st.cursor, 0)

    def test_clamp_empty_rows(self):
        st = UiState(tab=1, cursor=3)
        st.clamp(empty_data())
        self.assertEqual(st.cursor, 0)

    def test_row_count_per_tab(self):
        data = demo_data()
        self.assertEqual(UiState(tab=0).row_count(data), len(data.snapshot.positions))
        self.assertEqual(UiState(tab=1).row_count(data), len(data.trades))
        self.assertEqual(UiState(tab=2).row_count(data), len(discipline_rows(data.summary)))


class TestCli(unittest.TestCase):
    def _main(self, argv):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = dash.main(argv)
        return code, buf.getvalue()

    def test_render_each_tab(self):
        for tab in TABS:
            code, out = self._main(["--demo", "--render", tab, "--width", "90",
                                    "--height", "24"])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.rstrip("\n").split("\n")), 24)

    def test_render_rejects_unknown_tab(self):
        code, _ = self._main(["--demo", "--render", "不存在"])
        self.assertEqual(code, 2)

    def test_demo_mode_reads_only_examples(self):
        """demo 模式必须只读 examples/ —— 录 gif 时绝不能碰真实数据。"""
        data = load_dashboard_data(demo=True)
        self.assertTrue(data.demo)
        # Windows 上路径分隔符是 `\`,断言写死 "examples/" 会假失败
        self.assertIn("examples", Path(data.snapshot.source).parts)




class TestReportSkeletonIsConfigDriven(unittest.TestCase):
    """日报骨架必须来自 config,仓库不得预设任何研究范围。

    这是"通用美股纪律框架"定位的机器保证:换一份 watchlist.toml,
    日报的指数/板块/主题/分组/风险维度应当全部跟着换。
    """

    def setUp(self):
        import new_report

        self.nr = new_report
        self.cfg = {
            "report": {
                "indices": [{"name": "MY_INDEX"}],
                "sectors": [{"name": "我的板块", "etf": "ZZZZ"}],
                "themes": [{"name": "我的主题", "proxy": "YYYY"}],
                "technicals": ["TTTT"],
                "watch_levels": ["我的关键位"],
                "risk_dimensions": ["我的风险维度"],
            },
            "groups": [{"name": "我的分组", "symbols": ["AAAA", "BBBB"]}],
        }

    def test_all_sections_use_config_values(self):
        for builder, needle in (
            (self.nr.build_indices_section, "MY_INDEX"),
            (self.nr.build_sectors_section, "ZZZZ"),
            (self.nr.build_themes_section, "YYYY"),
            (self.nr.build_technicals_section, "TTTT"),
            (self.nr.build_group_news_section, "AAAA"),
            (self.nr.build_watchlist_section, "BBBB"),
            (self.nr.build_watch_levels_section, "我的关键位"),
            (self.nr.build_risk_section, "我的风险维度"),
        ):
            with self.subTest(builder=builder.__name__):
                self.assertIn(needle, builder(self.cfg))

    def test_missing_config_says_so_instead_of_substituting_defaults(self):
        """缺配置时应提示用户去填,而不是塞一份别人的研究偏好。"""
        for builder in (self.nr.build_sectors_section, self.nr.build_themes_section,
                        self.nr.build_group_news_section, self.nr.build_watchlist_section):
            with self.subTest(builder=builder.__name__):
                out = builder({})
                self.assertIn("watchlist.toml", out)

    def test_template_has_no_hardcoded_research_scope(self):
        """模板里不得再出现具体的行业/主题/个股名。"""
        tpl = (ROOT / "templates" / "daily-report.md").read_text(encoding="utf-8")
        for banned in ("七巨头", "AI 硬件", "半导体", "光通信", "核电",
                       "CCCC", "SMH", "IGV", "XLK", "AI 拥挤度"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, tpl)

    def test_template_placeholders_are_all_filled(self):
        """模板里的每个 {{TOKEN}} 都要有对应的 builder,否则会漏进产出物。"""
        import re

        tpl = (ROOT / "templates" / "daily-report.md").read_text(encoding="utf-8")
        tokens = set(re.findall(r"\{\{([A-Z_]+)\}\}", tpl))
        known = {"DATE", "PREV_DATE", "GENERATED_AT", "INDICES_SECTION", "SECTORS_SECTION",
                 "THEMES_SECTION", "TECHNICALS_SECTION", "GROUP_NEWS_SECTION",
                 "WATCHLIST_SECTION", "WATCH_LEVELS_SECTION", "RISK_SECTION"}
        self.assertEqual(tokens - known, set(), "模板里有无人填充的占位符")


if __name__ == "__main__":
    unittest.main()
