# 数据契约

本文件定义 meigu-ops 的**开源边界**和**数据格式**。
`AGENTS.md` §1 是契约的摘要,这里是完整规范。

---

## 1. 分层边界

判断任何文件归属,只问一个问题:

> **这是"怎么做",还是"做了什么"?**

| | 系统层(✅ 提交) | 用户层(❌ 永不提交) |
|---|---|---|
| 本质 | 方法论、规则、工具 | 事实记录、真实数字 |
| 例子 | `modes/*.md` 里的减仓纪律 | 昨天减了 AAAA 多少钱 |
| 例子 | `templates/daily-report.md` 的表格骨架 | 填好数据的 `reports/2026-08-17.md` |
| 例子 | `config/*.example.toml` 的占位配置 | `config/profile.toml` 里的真账户号 |
| 例子 | `scripts/stats.py` 的统计逻辑 | `data/trades.tsv` 的真实交易 |
| 换账户后 | 一行都不用改 | 全部作废 |

### 具体路径

```
✅ 系统层
   AGENTS.md CLAUDE.md README*.md DISCLAIMER.md LICENSE VERSION CHANGELOG.md Makefile
   .claude/skills/meigu-ops/SKILL.md
   modes/  templates/  scripts/  tests/  docs/  examples/
   config/*.example.toml
   .github/

❌ 用户层(全部 gitignore)
   config/profile.toml          真账户号、尺寸参数
   config/watchlist.toml        个人关注股
   data/trades.tsv              交易台账
   data/journal.md              交易日志
   data/lessons.md              经验档案
   data/snapshots/*.json        持仓/行情快照
   data/_archive/               历史归档
   reports/*.md                 收盘日报
   .claude/settings.local.json  MCP 权限白名单(含账户号与本机路径)
```

### 三条不可违反的规则

1. **完整账户号只存在于 `config/profile.toml`。**
   系统层文件里需要指代时写 `{{account_id}}`;对外展示只露后 4 位。
2. **绝对金额是用户层,百分比是系统层。**
   `modes/` 公开部分里所有尺寸都引用 `config` 参数或写百分比——因为
   "单笔 $50" 会暴露账户规模,"首仓 ≤ 计划仓位 1/2" 不会。
   这条约束的副作用是好的:纪律变得与账户规模无关。
3. **提交前跑 `make check-privacy`。** CI 里 `no-user-data.yml` 会再拦一次。

---

## 2. `data/trades.tsv` 交易台账

**唯一真相源。** 所有绩效统计、纪律验证、复盘数字都从这里来。
制表符分隔,10–11 列,顺序固定(第 11 列 `rule_ids` 可省略)。

```
date	checkpoint	symbol	side	qty	price	amount	reason_tag	pct_of_position	note	rule_ids
```

| 列 | 类型 | 说明 |
|---|---|---|
| `date` | `YYYY-MM-DD` | 成交日(ET) |
| `checkpoint` | 字符串 | 检查点标签:`09:12` `10:33` `13:03` `15:37` 或 `临时` |
| `symbol` | 大写代码 | 解析时自动转大写 |
| `side` | `buy` \| `sell` | 大小写不敏感 |
| `qty` | 小数 | 股数。分数股保留 5-6 位小数 |
| `price` | 小数 | **实际成交价**,不是下单时的报价。允许 `$` 前缀与千分位逗号 |
| `amount` | 小数 | 成交金额 |
| `reason_tag` | 枚举 | 见下方词表。**买卖标签不通用**,写错会报错 |
| `pct_of_position` | 小数或空 | 本笔占该仓位当时市值的 %。空值写 `-` |
| `note` | 自由文本 | 建议记下单时报价(用于估算滑点) |
| `rule_ids` | 分号分隔,**可省略** | 本笔依据了 `config/rules.toml` 的哪几条规则。第 11 列,旧台账(10 列)仍可读 |

### `reason_tag` 词表

**词表不在代码里,也不在本文档里** —— 它住在 `config/reason-tags.toml`(已 gitignore)。

**怎么细分交易理由,就是策略本身**:标签是盈亏归集的维度,你怎么分类,
决定了你能用数据问出哪些问题。所以本仓库只提供中立默认与细分方法,不提供答案。

| | 值 |
|---|---|
| 仓库默认(`config/reason-tags.example.toml`) | 买入 `建仓` / `加仓`;卖出 `减仓` / `清仓` |
| 你的真实词表 | `config/reason-tags.toml`(复制 `.example` 后自行细分) |
| 演示与测试固件 | `examples/sample-reason-tags.toml` |

约束:

- 买入与卖出标签**不得重名**(否则盈亏归集串台)。
- 标签不含制表符(台账是 TSV)。
- 改词表**不会**回溯改写 `data/trades.tsv` —— 旧行仍用旧标签,校验会报错。
  改名时同步处理台账,或保留旧标签直到它退出统计窗口。
- `scripts/preflight.py` 与台账解析都按该词表校验;写错会被拒绝并列出可用值。

### 校验与容错

- `#` 开头的行和空行会被跳过;`date` 开头的表头行会被跳过。
- 字段数、日期格式、`side` 值、`reason_tag` 归属、数字可解析性**全部强校验**,
  失败时报出**行号**。
- **不要为了让脚本跑过就删行。** 台账缺一笔,整段历史的 FIFO 配对都会错。
  用 `get_pnl_trade_history` / `get_equity_orders` 回补。

### 样本量从哪来(容易误解)

审计里的"决策事件数"来自**台账**,不是来自规则的 `evidence` 行数。

- 往 `evidence` 追加 20 条文字**不会**让样本数变成 20 —— 那只是叙事记录。
- `tag_compare` 的样本 = 台账里带相应 `reason_tag` 的**卖出笔数**(一笔卖出 = 一次退出决策)。
- 所以"规则要多久才够样本"取决于**你实际交易的频率**,不取决于你写了多少反思。

`rule_ids` 列让"这笔是依据哪条规则做的"可被追溯,是更细粒度归因的基础。

### 统计口径

`scripts/stats.py` 用 **FIFO** 配对买卖:

- 实现盈亏 = `(卖价 - 买价) × 配对股数`,只含已平仓部分,不含浮盈亏。
- 盈亏按**卖出行的 `reason_tag`** 归集;买入标签只统计笔数与金额。
- 持有天数是**自然日**,跨周末会虚高。
- 平仓配对 < 20 个时,脚本会明确声明"样本不足",不要解读百分比。

示例:`examples/sample-trades.tsv`(数据全为虚构)。

---

## 3. `data/journal.md` 交易日志

Markdown,倒序追加。结构由 `scripts/journal_compress.py` 强校验。

```markdown
# 交易日志(每日尾盘总结与反思 · 滚动压缩)

> 前言(可选):压缩策略说明

---

## YYYY-MM-DD(周X)· 一句话标题
{五块:当日操作 / 组合变动 / 市场背景 / 反思 / 明日输入}

---

## YYYY-MM-DD(周X)· ...
```

### 硬约束(每条都对应一次真实事故)

| 约束 | 事故 |
|---|---|
| 每个条目必须有 `## YYYY-MM-DD` 标题 | 2026-07-31 标题在编辑中丢失,正文变孤儿段落 |
| 条目必须严格按日期**倒序** | 2026-07-22 误锚定较早日期,条目错序数日未发现 |
| 全文 ≤ `journal.max_lines` | 2026-08-05 单进单出置换累积到 175 行才发现超限 |
| 无重复日期(合并条目除外) | — |

按周合并的条目用 `## YYYY-MM-DD~MM-DD(说明)` 形式,校验时豁免分层规则。

### 分层压缩

| 条目年龄(交易日) | 保留粒度 |
|---|---|
| ≤ `journal.full_detail_days`(默认 3) | 全细节 |
| ~ `journal.single_line_days`(默认 14) | 单行摘要 |
| 更早 | 按周/按轮合并 |

**脚本只判断"哪些该压、压到几行",摘要文字由人/LLM 写** —— 语义压缩不能自动化。

---

## 4. `data/snapshots/*.json` 持仓快照

每天一个文件,按检查点累积:

```json
{
  "date": "2026-08-17",
  "weekday": "周一",
  "day_info": "正常交易日",
  "checkpoints": {
    "10:33": {
      "captured_at_et": "2026-08-17 10:33:41",
      "portfolio": { "...": "get_portfolio 原始返回" },
      "positions": [ "get_equity_positions 原始返回" ],
      "quotes":    { "...": "get_equity_quotes 原始返回" }
    }
  }
}
```

**原始三块(`portfolio` / `positions` / `quotes`)建议原样存 MCP 返回值**,
不要提前加工——加工规则会变,原始数据不会。
快照是唯一能量化『现金闲置的机会成本』与『累计滑点』的数据源。

### `normalized` —— 给脚本用的归一化视图(必填,否则仪表盘读不到)

券商原始返回的字段名不稳定,所以 `scripts/dashboard.py` **只认 `normalized`,不猜原始字段**。
写快照时请一并提供:

```json
"normalized": {
  "total_value": 518.20,
  "cash": 92.80,
  "buying_power": 92.80,
  "equity_value": 425.40,
  "positions": [
    {"symbol": "AAAA", "qty": 0.472, "avg_cost": 398.50, "price": 438.00, "market_value": 206.74}
  ]
}
```

| 字段 | 用途 |
|---|---|
| `total_value` | BP 占比的分母(vs `cash.bp_target_pct`) |
| `buying_power` | 买入可用资金(**不是** `cash`,现金账户 T+1) |
| `equity_value` | 单一标的集中度的分母(vs `position.max_single_pct`) |
| `positions[].avg_cost` / `price` | 浮盈亏 |
| `positions[].market_value` | 集中度条、减仓占比复核 |

缺 `normalized` 时仪表盘会明确提示"快照缺少 normalized 块",而不是猜或静默显示 0。
示例见 `examples/sample-snapshot.json`(数字全为虚构)。

---

## 5. `data/lessons.md` 经验档案

复盘产出,倒序。每期:区间 / 组合表现 vs 基准 / 关键决策 / 三类错误分类 /
漏掉的机会 / 下期唯一要改的一件事。

**与系统层的分工:** `lessons.md` 存**证据和过程**(哪天、什么标的、什么结果);
提炼出的**规则**写进 `modes/*.md`。规则不带证据会在半年后失去说服力,
所以 `modes/*.md` 里的条目要带日期与标的引用——但不带绝对金额。
