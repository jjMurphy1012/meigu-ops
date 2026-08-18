# Mode: trade — 执行下单

**前置条件(硬性,不可绕过):** 必须已经完成 `modes/check.md` 或 `modes/premarket.md` 的分析,
并有明确、具体的理由。**没有分析支撑的下单一律禁止**(`AGENTS.md` 铁律 #2)。

**下单授权来自 `config/profile.toml` 的 `[execution]`,不来自本文件。**
本项目不预设任何人授权了自动下单 —— 授权是本地事实,clone 者不继承别人的授权。
执行前先读配置:

| 配置 | 含义 |
|---|---|
| `enabled = false`(仓库默认) | **禁止调用 `place_equity_order`**,只做分析与汇报 |
| `dry_run = true` | 走完分析与 `review`,输出"本应下什么单",但不 `place` |
| `require_confirmation = true` | 每笔都要等用户明确确认 |
| `max_order_usd` / `max_daily_usd` / `max_orders_per_day` | 硬性金额与笔数上限 |
| `kill_switch_file` 存在 | 一律禁止下单 |

"分析永远在下单之前"是**无法被任何配置关闭**的硬条件。

---

## Step 1 — 跑 preflight,拿到 ALLOW

**闸门是程序,不是自觉。** 把订单提案写成 JSON,交给 `scripts/preflight.py`:

```bash
python3 scripts/preflight.py --example > /tmp/order.json   # 看字段模板
# 填好实际值后:
python3 scripts/preflight.py --order-file /tmp/order.json
# 或直接 pipe:
echo '{...}' | python3 scripts/preflight.py --stdin --json
```

它检查这些**可机械判定**的硬约束(每条都对应过真实损失):

| 闸门 | 拦的是什么 |
|---|---|
| 紧急停止 / `execution.enabled` / 账户身份 | 授权与身份 |
| 市场时段 + 订单类型匹配 | 盘后下市价单、延长时段用错 `market_hours`、非交易日、半日市 |
| **意图时效**(默认 15 分钟) | 会话冻结/休眠后执行陈旧意图(2026-07-09 权限弹窗冻结 6h) |
| **报价时间戳熔断** | 分析与下单之间机器休眠(2026-07-13 靠这条刹住一单) |
| **减仓占比** | 套用标准尺寸到已缩水的仓位(2026-07-29 实际卖出 91%) |
| 残值仓 | 减完剩下无意义的零头 |
| 集中度 / 买力 / 现金底线 | 加仓后超单一标的上限、BP 不足、击穿现金底线 |
| 单日金额与笔数上限 | 单日累计失控 |
| 同标的当日重复 | 硬规则(不同标的多笔仍允许) |
| `ref_id` 格式与去重 | 重复下单 |
| 理由标签合法性 | 台账统计被脏数据污染 |
| **接入状态** | 跳过只读验证或演练就开真钱(状态机不只是提示) |
| 配置合法性 | 负数上限、0 笔数、非法 `live_mode` —— 等于没有上限 |
| 持仓股数 | 卖出股数超过实际持有 |
| 台账可读 | 台账损坏时 `ref_id` 去重失效,可能重复下单 |

**★ 订单里要写 `primary_rule_id`** —— 本笔**主要**依据的那**一条**市场判断类规则。
其余依据写进 `context_rule_ids`(数组,可省略)。

> 只有 `primary_rule_id` 决定尺寸。旧写法 `rule_ids` 已不再被识别 ——
> 用它会被直接 DENY,而不是静默按最低档放行。(静默降档比报错难查得多:
> 你以为写了依据,系统却当你没写。)

尺寸随证据强度自动缩放(`size_scale_*`):未声明或依据仍在观察期 → 按最低档;
依据已被数据支持的规则 → 满额。**不需要人工缩小尺寸** —— 超限时 preflight 会
直接告诉你允许的金额,按那个数重下即可。

引用 `refuted` / `retired` 的规则会被直接拒绝。

**判定含义:**

- `ALLOW` → 继续 Step 2。若 `require_confirmation = true`,仍需等用户确认。
- `DRY_RUN` → 走完 `review`,输出"本应下什么单",**不要 `place`**。
- `DENY` → **不许下**。不得绕过、不得"人工判断通过"。修正提案后重跑。

preflight **不判断该不该买** —— 标的好坏仍是 `modes/check.md` 的事。
它只回答:"如果现在下这笔单,有没有违反任何一条硬约束?"

> preflight 拦不住的部分仍需人工:该标的在延长时段是否合格
> (用 `get_equity_tradability`)、T+1 结算后的真实可用买力、以及判断本身对不对。

## Step 2 — review

```
review_equity_order(...)
```

**必须逐项读返回内容**,不是走个形式:

1. **报价时间戳熔断(最重要)**
   读 `market_data_disclosure` 里的 "Updated X PM ET" / venue 时间。
   **券商服务器时间戳是唯一可信时钟**——本机时钟可能因休眠停在旧时刻。
   时间戳与预期偏差大 → **立即中止,重新分析**。
   (2026-07-13 靠这条刹住了一张会排进次日晨开盘的市价单。)
2. 所有 warning / alert 逐条读完,不要忽略。
3. 核对方向、标的、金额/股数、订单类型、`market_hours` 与意图一致。

## Step 3 — place

```
place_equity_order(..., ref_id=<全新 UUID>)
```

- **每笔用全新 UUID 作 `ref_id`。** 只有重试同一笔时才复用同一个 ref_id(幂等)。
  生成:`uuidgen`。
- 返回 `state: unconfirmed / queued` **≠ 已成交**。

## Step 4 — 确认成交

1. 等 **30-60 秒**。
2. `get_equity_orders`(当日)看订单状态。
3. `get_equity_positions` 核对**实际成交价与股数**——不要用下单时的报价当成交价。
4. 若仍 `queued` 且已接近收盘 → 明确告知用户风险,必要时 `cancel_equity_order`。

## Step 5 — 事后立即完整汇报(铁律 #4)

```markdown
### 已执行:{买入/卖出} {标的} {金额或股数}

- **理由**:{具体依据,一到两句}
- **成交**:{股数} 股 @ ${成交价}(下单时报价 ${报价},滑点 {x}%)
- **占该仓位市值**:{%}
- **该标的最新持仓**:{股数} 股 / 成本 ${x} / 市值 ${y} / 浮盈亏 {z}%
- **组合最新**:总值 ${x} / 现金 ${y} / BP ${z}({BP占总值%},目标 <{目标}%)
- **持仓分布**:{各标的占股票市值 %}
- **ref_id**:{UUID 后 8 位}

*不构成投资建议。*
```

## Step 6 — 记入台账(不要漏)

追加一行到 `data/trades.tsv`。字段规范见 `docs/DATA_CONTRACT.md`,`reason_tag`
**必须用 `config/reason-tags.toml` 里定义的值**,不要自创、不要沿用文档里的示例标签。

```
date	checkpoint	symbol	side	qty	price	amount	reason_tag	pct_of_position	note	rule_ids
```

追加后跑一次校验:

```bash
make stats
```

若脚本报字段错误,立刻修正——**台账是后续所有绩效统计的唯一数据源**,
一行脏数据会污染整段历史。

---

## 失败模式速查

| 症状 | 真因 | 处置 |
|---|---|---|
| 工具调用一直没返回 | 大概率是**权限弹窗在等人**(不是网络挂起)。弹窗期间整个会话冻结,后续检查点全被阻塞 | 用户按 **Esc** 解冻;检查 `.claude/settings.local.json` 是否放行 review/place/cancel/get_equity_orders |
| 工具挂起 ~950-1050s 后 abort | MCP 偶发超时,原因未知 | 直接重试即可自愈;同一工具反复超时才升级调查 |
| review 时间戳远早于预期 | 机器在分析与下单之间休眠了 | 中止,重新分析。检查 `caffeinate` 与 `pmset -g batt` |
| 下单成功但持仓没变 | `queued` 未成交,或已过收盘 | `get_equity_orders` 查状态;必要时取消 |
| 减仓比例远超预期 | 套用标准尺寸到已缩水的仓位 | 见闸门三。已发生则按"残值仓要尽快处理"补救 |
