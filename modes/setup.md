# Mode: setup — 首次接入

**顺序原则:连接要早、只读;授权要晚、单独。**

这个项目的核心是"AI + 券商 MCP 自动交易"。如果把连接券商放在最后,用户会配置
半天才发现账户、权限或 MCP 根本不可用 —— 而那时 MCP 的问题和项目配置的问题
已经混在一起,分不清是谁的错。

但**连接成功 ≠ 获得真钱执行权限**。这两件事必须分开授权。

```bash
make setup            # 看当前处于哪一步、下一步做什么
make setup-checklist  # 看每一步的验收项
```

---

## Step 0 — 先问用户走哪条路

```
1. 连接 Robinhood MCP(推荐)—— 只读接入你的真实账户,不涉及任何下单权限
2. Demo 模式 —— 不连账户,用虚构固件把整套工作流跑一遍
```

**选 2 时不要索要任何账户信息**,直接:

```bash
make dashboard-demo
python3 scripts/stats.py --demo
```

只想看效果的人不该被迫授权券商。

---

## Step 1 · UNINITIALIZED — 环境与仓库

`make setup` 会自己查 Python 版本与仓库完整性。有问题按提示修。

---

## Step 2 · MCP_CONNECTED_READONLY — 券商只读验证

**这一步只确认三件事:连得上、连对账户、能拿到时间戳。**

引导用户安装并登录 Robinhood MCP,然后你执行下列**只读**调用:

| 调用 | 验证什么 |
|---|---|
| `get_accounts` | 能读取账户列表 |
| 在列表里定位目标子账户 | 账户存在,且账户号**唯一**(没有歧义) |
| `get_equity_positions` | 能读取持仓 |
| `get_portfolio` | 能读取 buying power |
| `get_equity_quotes` | 能拿到**带时间戳**的报价 |
| `review_equity_order` | 可调用(只审不下) |

### 硬性纪律

- **全程不得调用 `place_equity_order`。** 这一步没有任何下单需求。
- 遇到账户缺失、账户号与用户所说不符、连接中断 —— **停下来问用户,不要猜、不要继续**。
- 账户号只向用户展示后 4 位。

### 验证通过后写回

```bash
python3 scripts/setup.py --record-mcp --stdin <<'JSON'
{
  "account_id": "<实际读到的完整账户号>",
  "accounts_listed": true,
  "target_account_found": true,
  "target_account_unique": true,
  "positions_readable": true,
  "buying_power_readable": true,
  "quote_with_timestamp": true,
  "review_order_available": true,
  "place_order_not_called": true,
  "fail_closed_on_error": true,
  "notes": "读到 N 个账户;报价时间戳 …"
}
JSON
```

脚本会逐项校验,并核对读到的账户号与 `config/profile.toml` 是否一致 ——
**不一致会直接拒绝**,不会替用户选一个。任何一项不是 `true` 都不算通过。

> 只有**真的读到数据**才算验收。"检测到 MCP 配置文件"不是验收标准。

---

## Step 3 · PROFILE_READY — 账户与上限

```bash
cp config/profile.example.toml config/profile.toml
```

填入**刚刚验证过的**子账户号,并和用户一起定四个数:

| 参数 | 问用户 |
|---|---|
| `max_order_usd` | 单笔最多投多少? |
| `max_daily_usd` | 单日买入累计上限?(卖出不受此限 —— 不能让风控阻止你降低风险) |
| `max_orders_per_day` | 单日最多几笔? |
| `kill_switch_file` | 紧急停止用哪个文件?(默认 `data/HALTED`) |

**这四条是无条件的**:不随证据强度变化,任何规则都绕不过。
建议按"单日全损也能承受"的量级来定。

执行开关此时仍然全部关闭 —— 这一步只是配置,不是授权。

---

## Step 4 · STRATEGY_READY — 用户自己的策略

```bash
cp config/reason-tags.example.toml config/reason-tags.toml
cp config/rules.example.toml        config/rules.toml
cp modes/_strategy.example.md       modes/_strategy.md
```

**这是最花时间的一步,也是不能替用户做的一步。**

`modes/_strategy.example.md` 里全是问题,没有答案。陪用户过一遍那些问题,
把答案写成 `[[rule]]`。起步只写 2–3 条他最相信的 —— 十条没被检验过的规则,
不如两条被数据支持过的。

> ⚠️ **不要替用户编造市场判断类规则。** 没有市场规则时,系统只能跑分析与 dry-run。
> 这是设计如此,不是缺陷。

`make rules-check` 确认格式、标签引用、闸门引用都对。

---

## Step 5 · AUTOMATION_READY — dry-run 端到端演练

确认 `dry_run = true`,先**开一次演练**拿到 run id:

```bash
python3 scripts/setup.py --start-drill      # 打印 run id
```

然后完整跑一遍:盘前(`premarket`)→ 盘中(`check`)→ `preflight` → 模拟下单
(走完 `review`,**不 `place`**)→ 尾盘日志与台账(`journal`)→ 复盘审计(`review`)。

**跑 preflight 时在订单 JSON 里加 `"drill_run_id": "<run id>"`** ——
preflight 会把判定写进 `data/drill-runs.jsonl`,那才是演练真的发生过的证据。

跑通后写回:

```bash
python3 scripts/setup.py --record-drill --stdin <<'JSON'
{
  "premarket_ran": true, "check_ran": true, "preflight_ran": true,
  "order_simulated": true, "journal_written": true, "review_ran": true,
  "notes": "preflight 判定 …;台账写入 N 行"
}
JSON
```

**六个布尔值不足以记成完成。** 脚本还会核对:前三步确实就绪、当前确实处于
dry_run、以及**存在判定为 `DRY_RUN` 的 preflight 证据行**。
让被检查方自己出具检查结论,等于没有检查 —— 所以证据由闸门写,不由 agent 报。

演练的意义是把"配置对不对"和"链路通不通"分开验证。链路没跑通就开真钱,
出问题时你分不清是策略错了还是管道漏了。

---

## Step 6 · LIVE_AUTHORIZED — 单独授权真钱执行

前五步全部通过后才能到这里。**必须先向用户说清三件事**:

1. 将授权什么:agent 在运行中可直接提交真实订单,无需逐笔确认
2. 上限是多少:单笔 / 单日 / 笔数的具体数字
3. 怎么紧急停止:`touch data/HALTED` 立即全停

得到明确确认后:

```bash
# 先用 guarded:仓位统一压到最低档,不管规则状态多好
python3 scripts/setup.py --authorize-live guarded --approved

# 运行一段、确认健康之后,再考虑
python3 scripts/setup.py --authorize-live autonomous --approved
```

**guarded 与 autonomous 是两个独立决定**:先决定"开不开真钱",
再决定"放不放开仓位"。不要一次做完。

---

## 状态回溯

任何一步的前提被破坏(改了账户号、删了规则文件、规则文件读不出来),
`make setup` 会重新落回那一步。这是设计如此 —— 状态是**算出来的**,不是记下来的。

`data/setup-state.json` 只记录两件无法从文件推断的事:券商只读验证与演练的结果。
它属于用户层,已 gitignore。
