"""meigu-ops 共享库 —— 路径、配置、交易日历、台账解析。

仅使用 Python 标准库(3.11+,依赖 tomllib)。
"""

from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
TEMPLATES_DIR = ROOT / "templates"

TRADES_TSV = DATA_DIR / "trades.tsv"
JOURNAL_MD = DATA_DIR / "journal.md"

ET = "America/New_York"


def rel_to_root(path: Path) -> str:
    """相对仓库根的路径;仓库外的路径(如测试临时文件)返回绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)

# ---------------------------------------------------------------- 理由标签词表
# ⚠️ 词表**不在代码里硬编码** —— 怎么细分交易理由是用户的策略,不是本仓库的。
# 真词表在 config/reason-tags.toml(已 gitignore);缺失时回退到下面的中立默认。
# 完整说明见 config/reason-tags.example.toml。


@dataclass(frozen=True)
class Vocabulary:
    """交易理由标签词表。

    标签是**盈亏归集的维度**:stats 按卖出行的标签归集 FIFO 实现盈亏,
    所以词表决定了"你能检验哪些假设"。怎么细分由用户定。
    """

    buy: tuple[str, ...]
    sell: tuple[str, ...]
    source: str = "内置默认"
    is_example: bool = True

    @property
    def all(self) -> tuple[str, ...]:
        return self.buy + self.sell

    def expected_for(self, side: str) -> tuple[str, ...]:
        return self.buy if side == "buy" else self.sell

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.buy:
            errs.append("tags.buy 为空 —— 至少需要一个买入标签")
        if not self.sell:
            errs.append("tags.sell 为空 —— 至少需要一个卖出标签")
        overlap = set(self.buy) & set(self.sell)
        if overlap:
            errs.append(f"买卖标签重名会让盈亏归集串台:{sorted(overlap)}")
        for tag in self.all:
            if "\t" in tag:
                errs.append(f"标签不能含制表符(台账是 TSV):{tag!r}")
            if not tag.strip():
                errs.append("标签不能为空白")
        return errs


# 中立默认:只描述动作,不含任何市场判断含义。
DEFAULT_VOCABULARY = Vocabulary(buy=("建仓", "加仓"), sell=("减仓", "清仓"))

_vocab_cache: Vocabulary | None = None


def load_vocabulary(
    required: bool = False, refresh: bool = False, path: Path | None = None
) -> Vocabulary:
    """读 config/reason-tags.toml;缺失时回退 .example,再缺则用内置默认。

    `path` 给定时直接读该文件(演示与测试固件用),不走缓存。
    """
    global _vocab_cache
    if path is None and _vocab_cache is not None and not refresh:
        return _vocab_cache

    if path is not None:
        candidates = [(path, True)]
    else:
        candidates = [
            (CONFIG_DIR / "reason-tags.toml", False),
            (CONFIG_DIR / "reason-tags.example.toml", True),
        ]

    for path_, is_example in candidates:
        if not path_.exists():
            continue
        path = path_
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        tags = raw.get("tags", {})
        vocab = Vocabulary(
            buy=tuple(tags.get("buy") or ()),
            sell=tuple(tags.get("sell") or ()),
            source=rel_to_root(path),
            is_example=is_example,
        )
        errs = vocab.validate()
        if errs:
            raise ConfigError(f"{rel_to_root(path)} 词表有问题:\n  " + "\n  ".join(errs))
        if required and is_example and path.parent == CONFIG_DIR:
            raise ConfigError(
                "config/reason-tags.toml 不存在,当前用的是样例词表。\n"
                "  执行:cp config/reason-tags.example.toml config/reason-tags.toml 并按自己的打法细分。"
            )
        if path.parent == CONFIG_DIR:
            _vocab_cache = vocab
        return vocab

    _vocab_cache = DEFAULT_VOCABULARY
    return _vocab_cache

TRADE_COLUMNS = (
    "date",
    "checkpoint",
    "symbol",
    "side",
    "qty",
    "price",
    "amount",
    "reason_tag",
    "pct_of_position",
    "note",
)


class ConfigError(RuntimeError):
    pass


class LedgerError(RuntimeError):
    pass


# ------------------------------------------------------------------------ 配置
def load_config(name: str = "profile", required: bool = True) -> dict:
    """读 config/{name}.toml;不存在时回退到 .example.toml 并标记 is_example。"""
    real = CONFIG_DIR / f"{name}.toml"
    example = CONFIG_DIR / f"{name}.example.toml"

    path = real if real.exists() else example
    if not path.exists():
        raise ConfigError(f"找不到 {real} 或 {example}")

    with path.open("rb") as fh:
        cfg = tomllib.load(fh)

    cfg["_source"] = str(path.relative_to(ROOT))
    cfg["_is_example"] = path == example

    if required and cfg["_is_example"]:
        raise ConfigError(
            f"config/{name}.toml 不存在,当前用的是样例配置。\n"
            f"  执行:cp config/{name}.example.toml config/{name}.toml 并填写真实值。\n"
            f"  样例里的占位值绝不能用于下单。"
        )
    return cfg


# -------------------------------------------------------------------- 交易日历
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """该月第 n 个 weekday(0=周一)。n 为负数表示从月末倒数。"""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    # 从月末倒数
    if month == 12:
        last = dt.date(year, 12, 31)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - dt.timedelta(days=offset + 7 * (-n - 1))


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(date: dt.date) -> dt.date:
    """NYSE 观察日规则:周六提前到周五,周日推到周一。"""
    if date.weekday() == 5:
        return date - dt.timedelta(days=1)
    if date.weekday() == 6:
        return date + dt.timedelta(days=1)
    return date


def market_holidays(year: int) -> dict[dt.date, str]:
    """NYSE/Nasdaq 全天休市日。

    注意:不含临时休市(国葬、飓风等不可预测的关闭)。
    """
    h: dict[dt.date, str] = {}
    h[_observed(dt.date(year, 1, 1))] = "元旦"
    h[_nth_weekday(year, 1, 0, 3)] = "马丁·路德·金日"
    h[_nth_weekday(year, 2, 0, 3)] = "华盛顿诞辰日"
    h[_easter(year) - dt.timedelta(days=2)] = "耶稣受难日"
    h[_nth_weekday(year, 5, 0, -1)] = "阵亡将士纪念日"
    h[_observed(dt.date(year, 6, 19))] = "六月节"
    h[_observed(dt.date(year, 7, 4))] = "独立日"
    h[_nth_weekday(year, 9, 0, 1)] = "劳动节"
    h[_nth_weekday(year, 11, 3, 4)] = "感恩节"
    h[_observed(dt.date(year, 12, 25))] = "圣诞节"
    return h


def half_days(year: int) -> dict[dt.date, str]:
    """提前收盘日(13:00 ET)。"""
    hd: dict[dt.date, str] = {}
    holidays = market_holidays(year)

    # 感恩节次日
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    hd[thanksgiving + dt.timedelta(days=1)] = "感恩节次日"

    # 平安夜:是工作日且本身不是休市日
    xmas_eve = dt.date(year, 12, 24)
    if xmas_eve.weekday() < 5 and xmas_eve not in holidays:
        hd[xmas_eve] = "平安夜"

    # 独立日前一日:仅当 7/4 落在周二~周五(此时 7/3 是工作日且非休市日)
    jul3 = dt.date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in holidays:
        hd[jul3] = "独立日前一日"

    return hd


@dataclass
class DayInfo:
    date: dt.date
    is_trading_day: bool
    is_half_day: bool
    reason: str

    @property
    def close_et(self) -> str | None:
        if not self.is_trading_day:
            return None
        return "13:00" if self.is_half_day else "16:00"


def day_info(date: dt.date) -> DayInfo:
    if date.weekday() >= 5:
        return DayInfo(date, False, False, "周末")
    holidays = market_holidays(date.year)
    if date in holidays:
        return DayInfo(date, False, False, f"休市:{holidays[date]}")
    hd = half_days(date.year)
    if date in hd:
        return DayInfo(date, True, True, f"半日市:{hd[date]}(13:00 ET 收盘)")
    return DayInfo(date, True, False, "正常交易日")


def is_trading_day(date: dt.date) -> bool:
    return day_info(date).is_trading_day


def prev_trading_day(date: dt.date) -> dt.date:
    d = date - dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def next_trading_day(date: dt.date) -> dt.date:
    d = date + dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """(start, end] 区间内的交易日数量。"""
    if end <= start:
        return 0
    count, d = 0, start + dt.timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            count += 1
        d += dt.timedelta(days=1)
    return count


def today_et() -> dt.date:
    """ET 当前日期 —— 绝不用本机时区,本机时钟会漂移(见 modes/_mechanics.md §4)。"""
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(ET)).date()


def now_et() -> dt.datetime:
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(ET))


WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ------------------------------------------------------------------ 台账解析
@dataclass
class Trade:
    line_no: int
    date: dt.date
    checkpoint: str
    symbol: str
    side: str  # buy | sell
    qty: float
    price: float
    amount: float
    reason_tag: str
    pct_of_position: float | None
    note: str


def _parse_float(raw: str, field: str, line_no: int) -> float:
    cleaned = raw.strip().lstrip("$").replace(",", "").rstrip("%")
    try:
        return float(cleaned)
    except ValueError as exc:
        raise LedgerError(f"第 {line_no} 行:{field} 无法解析为数字:{raw!r}") from exc


def parse_trades(path: Path = TRADES_TSV, vocab: Vocabulary | None = None) -> list[Trade]:
    """解析 data/trades.tsv。校验失败抛 LedgerError 并指出行号。

    台账缺一笔,整段历史的 FIFO 配对都会错 —— 所以这里宁可报错也不静默跳过。
    `vocab` 缺省时从 config/reason-tags.toml 读(见 load_vocabulary)。
    """
    vocab = vocab or load_vocabulary()
    if path is None:
        path = TRADES_TSV
    if not path.exists():
        return []

    trades: list[Trade] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = line.split("\t")
        if cells[0].strip() == "date":  # 表头
            continue
        if len(cells) != len(TRADE_COLUMNS):
            raise LedgerError(
                f"第 {line_no} 行:应有 {len(TRADE_COLUMNS)} 个制表符分隔字段,"
                f"实际 {len(cells)} 个。字段顺序:{'/'.join(TRADE_COLUMNS)}"
            )

        (
            date_s,
            checkpoint,
            symbol,
            side,
            qty_s,
            price_s,
            amount_s,
            reason_tag,
            pct_s,
            note,
        ) = (c.strip() for c in cells)

        try:
            date = dt.date.fromisoformat(date_s)
        except ValueError as exc:
            raise LedgerError(f"第 {line_no} 行:日期格式应为 YYYY-MM-DD,实际 {date_s!r}") from exc

        side_norm = side.lower()
        if side_norm not in ("buy", "sell"):
            raise LedgerError(f"第 {line_no} 行:side 必须是 buy 或 sell,实际 {side!r}")

        if reason_tag not in vocab.all:
            raise LedgerError(
                f"第 {line_no} 行:reason_tag {reason_tag!r} 不在词表内"
                f"({vocab.source})。\n"
                f"  买入可用:{'/'.join(vocab.buy)}\n"
                f"  卖出可用:{'/'.join(vocab.sell)}"
            )
        expected = vocab.expected_for(side_norm)
        if reason_tag not in expected:
            raise LedgerError(
                f"第 {line_no} 行:{side_norm} 不应使用标签 {reason_tag!r};"
                f"可用:{'/'.join(expected)}"
            )

        trades.append(
            Trade(
                line_no=line_no,
                date=date,
                checkpoint=checkpoint,
                symbol=symbol.upper(),
                side=side_norm,
                qty=_parse_float(qty_s, "qty", line_no),
                price=_parse_float(price_s, "price", line_no),
                amount=_parse_float(amount_s, "amount", line_no),
                reason_tag=reason_tag,
                pct_of_position=(
                    _parse_float(pct_s, "pct_of_position", line_no) if pct_s and pct_s != "-" else None
                ),
                note=note,
            )
        )

    trades.sort(key=lambda t: (t.date, t.line_no))
    return trades
