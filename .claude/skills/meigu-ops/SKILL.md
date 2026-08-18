---
name: meigu-ops
description: >-
  美股自管账户的决策指挥中心 —— 盘前分析、盘中检查点买卖决策、下单执行、收盘日报、
  尾盘交易日志、周期复盘、绩效统计。当用户要分析美股持仓/大盘、做买卖决策、制定盘前或
  盘中计划、执行下单、写收盘日报或交易日志、复盘总结经验、查交易统计时使用。
  中文输出,操作用中性语言并声明不构成投资建议。
arguments: mode
user_invocable: true
user-invocable: true
argument-hint: "[premarket | check | trade | daily | journal | review | stats | doctor]"
license: MIT
---

# meigu-ops — 路由

本文件只做**路由**。真正的指令在 `modes/*.md`,按需读**一个**,不要一次全读——
这是本项目的上下文经济设计。

## 执行前置(每个 mode 都要做)

1. 读 `AGENTS.md`(铁律 / 数据分层 / 不可信内容 / 输出约定)。
2. 读 `modes/_mechanics.md`(平台机制与执行陷阱 —— **不含任何策略**)。
3. 读**用户自己的策略层**:
   - `config/profile.toml` —— 账户号、尺寸参数、现金目标、`[execution]` 下单授权
   - `config/reason-tags.toml` —— 写台账用的理由标签词表
   - `config/rules.toml` —— 可检验的纪律规则(只按 `status` 为
     `enforced` / `supported` / `hypothesis` 的规则决策;`refuted` / `retired` 仅作记录)
   - `modes/_strategy.md` —— 散文形式的完整策略(若存在)
4. 再读本次 mode 对应的 `modes/<mode>.md`。

> ⚠️ **本仓库不提供任何交易策略。** 上面第 3 步的四个文件都已 gitignore,
> 对应的模板是 `*.example.*`。若它们不存在,说明用户还没有定义自己的策略 ——
> **此时不要用模板里的占位值做决策,也不要自行编造规则**,而是停下来提示用户先完成:
>
> ```bash
> cp config/profile.example.toml      config/profile.toml
> cp config/reason-tags.example.toml  config/reason-tags.toml
> cp config/rules.example.toml        config/rules.toml
> cp modes/_strategy.example.md       modes/_strategy.md
> ```
>
> `execution.enabled` 默认 `false` —— clone 本仓库不继承任何人的下单授权。

## 标签的唯一来源

写 `data/trades.tsv` 的 `reason_tag`,以及在分析里给操作归类时,
**必须使用 `config/reason-tags.toml` 里定义的标签**。

不要凭记忆写、不要自创、不要沿用文档里看到的任何示例标签 ——
`scripts/preflight.py` 与台账解析都会按该词表校验,写错会被直接拒绝。
不确定当前词表是什么就先读那个文件。

## 模式路由

| 输入 | Mode | 文件 | 用途 |
|---|---|---|---|
| (空) | `discovery` | — | 显示菜单 |
| `premarket` / `盘前` | `premarket` | `modes/premarket.md` | 盘前分析,定今日方案与候选 |
| `check` / `盘中` | `check` | `modes/check.md` | 盘中检查点:买/卖/不动决策 |
| `trade` / `下单` | `trade` | `modes/trade.md` | 执行下单 + 事后汇报 + 记台账 |
| `daily` / `日报` | `daily` | `modes/daily.md` | 收盘日报(15 节完整版) |
| `journal` / `日志` | `journal` | `modes/journal.md` | 尾盘交易日志 + 台账 + 压缩 |
| `review` / `复盘` | `review` | `modes/review.md` | 周/月复盘,提炼经验回写手册 |
| `stats` / `统计` | `stats` | `modes/stats.md` | 台账绩效统计与解读 |
| `doctor` / `自检` | `doctor` | — | 直接跑 `make doctor` 并解读输出 |

**意图推断**(输入不是上述关键词时):

- 只给了股票代码(如 `AAAA`)或"看一下 XXX" → `check`,聚焦该标的。
- 提到"日报""昨夜""大盘怎么样" → `daily`。
- 提到"买""卖""加仓""减仓""清仓" → `check`(先分析),分析通过后自动转 `trade`。
- 提到"总结""反思""教训""经验" → `review`。
- 提到"赚了多少""胜率""统计" → `stats`。
- 都不像 → 显示 discovery 菜单。

## Discovery 菜单(无参数时输出)

```
meigu-ops v5 · 美股决策指挥中心

  /meigu-ops premarket   盘前分析 —— 定今日方案、候选与触发位
  /meigu-ops check       盘中检查点 —— 买 / 卖 / 不动的决策
  /meigu-ops trade       执行下单 —— review → place → 汇报 → 记台账
  /meigu-ops daily       收盘日报 —— 15 节完整市场复盘
  /meigu-ops journal     尾盘日志 —— 当日总结 + 台账 + 自动压缩
  /meigu-ops review      周期复盘 —— 提炼经验并回写纪律手册
  /meigu-ops stats       绩效统计 —— 胜率 / 持有天数 / 标签分布
  /meigu-ops doctor      环境自检 —— 配置 / 防休眠 / 交易日 / 台账完整性

也可以直接说人话:"AAAA 现在能加吗" / "写今天的日报" / "这周复盘一下"

不构成投资建议。
```

## 每日标准节奏(ET)

| 时刻 | Mode | 说明 |
|---|---|---|
| 9:12 | `premarket` | 定今日方案 + 调度自愈 |
| 10:33 | `check` | 开盘一小时波动消化后,当日第一个信息相对完整的时点 |
| 13:03 | `check` | 上午趋势确认/反转;13:00 国债拍卖影响利率类标的 |
| 15:37 | `check` | 盘中最后一个可靠窗口(各时段的门槛差异由 `_strategy.md` 定义) |
| 16:06 | `journal` | 写交易日志 + 记台账 + 压缩 |

催化事件(NFP / FOMC / CPI / 重磅财报)按日历另设专项检查点。

> ⚠️ **当前是"有人在场的自动化"**:节奏 cron 是会话级的,CLI 退出即丢;
> 仓库还没有持久调度器,也无法从代码层面阻止 agent 绕过 preflight 直调 MCP。
> 开了 `execution.enabled` 只代表"这次运行里可以真下单",不代表无人值守。

**节奏 cron 均为会话级且 7 天自动过期。** 9:12 的 `premarket` 负责每日自愈重建;
若整个会话重启,新会话首次做交易分析时应把五点节奏全部重建
(重建时必须把时间自检写进每条 prompt,见 `_mechanics.md` §4-§5)。

**用户可随时提前收官**("这是今天最后一次操作,你自己判断"):当场做出当日最终判断;
之后即使原定检查点仍按 cron 触发,也只核对行情 + 汇报,不再下新单,
除非出现真正的极端破位(用户没有免除止损纪律,只是收回了"找机会"的主动动作)。

## 可用脚本(机械活不要手算)

```bash
make doctor          # 环境自检
make trading-day     # 今天是不是交易日 / 下一个交易日
make report          # 生成当日日报骨架到 reports/
make journal-check   # 检查 data/journal.md 是否超行数上限
make journal-compress# 分层压缩交易日志
make stats           # 台账统计(FIFO 实现盈亏 / 胜率 / 标签分布)
make preflight-example  # 订单 JSON 模板
make preflight       # ★ 下单前置检查 → ALLOW / DRY_RUN / DENY
make dashboard       # 只读仪表盘 TUI(组合 / 台账 / 纪律审计)
make rules-check     # 规则格式 / 标签引用 / 闸门引用 / 散文同步
python3 scripts/rules.py --record-evidence <id> "…"   # 每日记证据(免批准)
python3 scripts/rules.py --set-status <id> supported --approved   # 改状态(需用户批准)
make check-privacy   # 提交前隐私检查
```

**下单必经 `preflight`。** 它返回 `DENY` 就是不许下,不得绕过或"人工判断通过"
(详见 `modes/trade.md` Step 1)。

**订单要写 `rule_ids`** —— 本笔依据了 `config/rules.toml` 的哪几条规则。
尺寸随证据强度自动缩放:未声明或依据仍在观察期按最低档,依据已被数据支持的规则可满额。
超限时 preflight 会直接给出允许金额,按那个数重下即可 —— **不需要人工换算**。
