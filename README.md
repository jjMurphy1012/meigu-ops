<h1 align="center">meigu-ops</h1>

<p align="center">
  <strong>把你自己的交易纪律,外化成一个能被数据检验的系统</strong><br>
  <em>面向单人自管的美股账户 · Robinhood MCP + Claude Code</em>
</p>

<p align="center">
  简体中文 | <a href="README.en.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/dependencies-none-2ea44f?style=flat" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/tests-274%20passing-2ea44f?style=flat" alt="274 tests">
  <a href="https://claude.com/claude-code"><img src="https://img.shields.io/badge/Built_with-Claude_Code-000?style=flat&logo=anthropic&logoColor=white" alt="Built with Claude Code"></a>
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT">
  <a href="DISCLAIMER.md"><img src="https://img.shields.io/badge/%E2%9A%A0%EF%B8%8F-%E4%B8%8D%E6%9E%84%E6%88%90%E6%8A%95%E8%B5%84%E5%BB%BA%E8%AE%AE-critical" alt="Not investment advice"></a>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="meigu-ops 演示" width="900">
</p>

<p align="center">
  <sub>演示里的每一个数字、每一个标签都是虚构占位(<code>examples/</code> 固件)。<br>
  最后一段重放了一次真实事故形态:意图「部分减仓」,但仓位已缩水到只剩一个标准尺寸 ——
  闸门算出会卖掉 <strong>90.9%</strong>,拒绝下单。</sub>
</p>

---

## ⚠️ 先说清楚这个项目不提供什么

**它不提供交易策略。**

你不会在这个仓库里找到"什么时候买"、"现金留多少"、"单一标的上限多少"的答案。
不是因为作者藏私,而是因为**那些答案对你没有意义** —— 账户规模、风险承受度、
标的池、能投入的盯盘时间,每个人都不同。照抄一套不属于你的规则比没有规则更危险:
你会在不理解它为什么成立的情况下执行它,然后在它失效时既发现不了、也无法修正。

仓库提供的是**让你自己的纪律能被数据检验、并因此不断修正的机制**:

```
你写下规则 ──► 每日决策时被引用 ──► 交易进台账 ──► 复盘时被数据审计
     ▲                                                     │
     └────────── 升级 / 降级 / 退役(需你批准)◄───────────┘
```

## 我为什么做这个

2026 年 6 月，我在一次财报爆表的次日开盘一次性满仓追进当时最热的主线。
接下来两周，市场把那一轮涨幅原样还了回来。

有意思的是后面发生的事：靠几条很朴素的纪律——在反弹里减而不是在下跌里割、
先砍最弱的一环、在已知的二元事件前把现金提上来——我把损失控制在个位数百分比，
同期我持仓所在的板块指数跌了 7% 以上。

**问题是那几条纪律只存在于我脑子里。** 而脑子会忘、会侥幸、会在连续第 20 天
盯盘之后变得懈怠。更糟的是，一个月后我自己复盘时发现了相反的毛病：
同一套"谨慎"让我在该出手的时候反复等待，现金长期空转——
那笔亏损同样真实，只是它不出现在盈亏表里。

两个方向的失败让我确认了一件事：**问题不在于我的规则对不对，
而在于我没有任何机制去知道它们对不对。**

于是有了这个项目。它把纪律从脑子里搬进文件，把机械的检查从自觉搬进程序，
最后——也是最花时间的一部分——把"这条规则到底成不成立"变成一个能被台账数据
回答的问题，而不是一个凭印象争论的问题。

> 你在这个仓库里找不到我的那几条纪律。它们对你没有意义，
> 而且我自己也在不断修正它们。**留下的是那套让纪律能被检验、能被推翻的机制。**

## 它做什么

```
9:12   /meigu-ops premarket   盘前 → 候选清单 + 触发位 + 防守预案 + 今日不做什么
10:33  /meigu-ops check       盘中 → 三问过滤 → 买 / 卖 / 不动
13:03  /meigu-ops check
15:37  /meigu-ops check
16:06  /meigu-ops journal     尾盘 → 交易日志 + 台账 + 分层压缩 + 证据累积
       /meigu-ops daily       收盘日报(15 节,骨架由你的配置驱动)
       /meigu-ops review      周期复盘 → 用数据审计你的每一条规则
       /meigu-ops stats       FIFO 实现盈亏 / 标签绩效 / 规则审计
```

它不预测市场。它做的是另一件事:**保证今天的判断不会比上次做得更差。**

## 核心特性

| 特性 | 说明 |
|---|---|
| **策略层完全私有** | 标签词表、纪律规则、散文策略、账户参数全部 gitignore。仓库只有 `*.example.*` 模板(问题清单,零答案) |
| **规则可被数据审计** | 把纪律写成可证伪的条目,`make stats` 用台账数据判定它成立与否。**从未被数据支持过的规则是包袱,不是资产** |
| **审计拒绝草率结论** | 结论方向与证据强度分离:< 10 个决策事件不下结论,10–19 只给"弱支持/弱反驳",≥ 20 才可**建议**改状态 —— 而任何状态变更都需要你批准 |
| **仓位随证据自动缩放** | 证据强度决定**尺寸**而不是决定能否交易 —— 否则第一天就死锁(没数据→不许交易→永远攒不到数据)。未验证的假设按 40% 仓位跑,弱支持 70%,已支持满额。**全自动,无需人工缩小** |
| **样本单位是决策事件** | 一笔卖单匹配三个历史买入批次,仍然只算**一次**退出决策 —— 不是三个样本 |
| **确定性下单闸门** | `preflight.py` 把意图时效、券商报价时间戳熔断、减仓占比、集中度、单日上限、`ref_id` 去重做成**程序**而非散文,返回 `ALLOW`/`DRY_RUN`/`DENY` |
| **下单授权默认关闭** | `execution.enabled = false` 是仓库默认值 —— clone 不继承任何人的授权。授权是本地事实 |
| **平台机制知识库** | `modes/_mechanics.md` 汇总 Robinhood MCP + Claude Code 会以什么方式把正确决策变成失败执行:结算、时间戳熔断、休眠截杀、时区漂移、权限弹窗冻结会话 |
| **只读仪表盘 TUI** | 组合 / 台账 / 规则审计三页。curses,零依赖,整个 UI 是纯函数因此被测试完整覆盖 |
| **日报骨架 config 驱动** | 指数、板块、主题、分组、风险维度全部来自你的配置。模板里没有任何行业或个股名 |
| **零依赖** | Python 3.11+ 标准库。`tomllib` 读配置,`zoneinfo` 处理 ET |

## 快速开始

```bash
git clone https://github.com/jjMurphy1012/meigu-ops.git
cd meigu-ops

cp config/profile.example.toml      config/profile.toml       # 账户与尺寸参数
cp config/watchlist.example.toml    config/watchlist.toml     # 关注池与日报骨架
cp config/reason-tags.example.toml  config/reason-tags.toml   # 你的理由标签词表
cp modes/_strategy.example.md       modes/_strategy.md        # 你的策略(回答里面的问题)
cp config/rules.example.toml        config/rules.toml         # 可检验的规则条目

make doctor        # 环境自检
make rules-check   # 规则格式 / 标签引用 / 闸门引用
make test          # 274 个测试
make report        # 生成当日日报骨架
```

**第四步是这个项目的重点,也是最花时间的一步。** `modes/_strategy.example.md` 里
全是问题,没有答案 —— 那些答案必须由你自己写。起步建议:**先只写 2–3 条你最相信的规则**。
十条没被检验过的规则,不如两条被数据支持过的。

完整配置见 [docs/SETUP.md](docs/SETUP.md)。

## 脚本层

| 命令 | 作用 | 为什么不让 LLM 做 |
|---|---|---|
| `make doctor` | 配置 / 时钟漂移 / 防休眠 / 权限白名单 / 台账 / 日志 | 每一项都对应一次真实故障 |
| `make preflight` | 下单前置检查 → `ALLOW`/`DRY_RUN`/`DENY` | 最有后果的检查最不该靠自觉 |
| `make rules-check` | 规则格式、标签引用、闸门引用、散文同步 | 写错闸门名会让规则显示「程序强制」但其实无人把守 |
| `rules.py --record-evidence` / `--set-status` | 机械化地写回规则(改状态需 `--approved`) | 让 agent 手改 TOML 会静默破坏审计 |
| `make stats` | FIFO 实现盈亏 / 标签绩效 / **规则审计** | 复盘最大的陷阱是凭印象 |
| `make dashboard` | 只读仪表盘 TUI | 每天要看的三张表,一屏看完 |
| `make trading-day` | 交易日 / 半日市 / 上下一交易日 | 观察日顺延、耶稣受难日人算会错,错一次毁一天 |
| `make journal-check` | 日志结构:标题 / 倒序 / 孤儿段落 / 行数 | `Edit` 返回成功 ≠ 文档结构对了 |
| `make check-privacy` | 提交前隐私检查 | 人会忘,而这个错误不可逆 |

## 关于"全自动"的边界

把 `config/profile.toml` 的 `[execution]` 三个开关打开，意味着：

✅ **agent 在一次运行中可以真的下单**，无需逐笔确认
✅ 每笔仍必经 `preflight`，四条硬上限与 kill switch 无条件生效
✅ 仓位按证据强度自动缩放，无需人工换算

**它不意味着以下任何一条**（这些是当前的真实缺口）：

❌ 五个检查点已被安装成**持久调度**——目前依赖会话级 cron，CLI 退出即丢，
   需要盘前自愈重建。仓库尚无 `scheduler install/status` 之类的命令
❌ agent **无法绕过** `preflight` 直接调用券商 MCP——这一层只能靠
   `.claude/settings.local.json` 的权限白名单和流程约束，仓库内无法强制
❌ 成交结果自动回写台账并驱动审计——记台账仍是 `journal` mode 的动作

所以现阶段准确的说法是**「有人在场的自动化」**：你需要保持会话存活，
而系统负责让每一笔决策都过一遍确定性闸门、并把证据攒进规则里。

真正的无人值守还需要持久调度器与唯一下单网关——那是下一阶段的工作。

## 数据边界

```
┌───────── ✅ 系统层(可开源)─────────┐  ┌──── ❌ 用户层(永不进仓)────┐
  AGENTS.md   modes/_mechanics.md         config/profile.toml     账户与授权
  modes/{premarket,check,trade,...}.md    config/reason-tags.toml 你的标签词表
  templates/  scripts/  tests/  docs/     config/rules.toml       你的规则
  config/*.example.*                      modes/_strategy.md      你的策略
  modes/_strategy.example.md              data/  reports/         台账/日志/日报
└─────────────────┬───────────────────┘  └──────────┬────────────┘
                  │                                 │
          .gitignore 强制        make check-privacy + CI no-user-data
```

判断标准是一个问题:**这是"怎么做"还是"做了什么"?**
"怎么细分交易理由、相信哪些规则"属于后者。完整契约见
[docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md)。

## 适用范围

- **券商**:只对接 Robinhood MCP,不做多券商抽象。
- **市场**:美股。交易日历按 NYSE/Nasdaq 规则计算(不含临时休市)。
- **风格无关**:日报骨架、标签词表、规则全部由你的配置驱动 —— 仓库不预设任何研究方向。
- **规模**:为单人自管的小额账户设计(分数股、市价单滑点、现金账户 T+1 都在考虑之内)。

## 文档

| 文件 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | 唯一共享上下文:铁律 / 数据契约 / 不可信内容 / 活文档维护 |
| [docs/SETUP.md](docs/SETUP.md) | 安装、配置、AI CLI 接入、券商权限与防休眠 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 设计原则、数据流、关键技术决策 |
| [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) | 开源边界 + 台账 / 日志 / 快照 / 规则的格式规范 |
| [CHANGELOG.md](CHANGELOG.md) | 系统层演进(策略变更不进这里) |
| [DISCLAIMER.md](DISCLAIMER.md) | 免责声明 |

## 设计取向

- **纪律可以被删除。** 定期审计;从未被数据支持的规则是包袱。
- **"不动"必须是结论,不是回避。** 决定不操作时要逐个点名每个候选被什么理由挡住。
- **闲置也是成本。** 它只是不出现在盈亏表里,所以复盘要对称地审计"漏掉的机会"。
- **绝不编造数据。** 取不到就写"暂无可靠数据"。留白永远优于编造。
- **判断错 ≠ 规则错。** 依据充分但市场走反不该改规则 —— 那是过拟合到噪音。

## ⚠️ 免责

**本项目不构成投资建议。** 它是个人研究与流程工具,不预测市场,不推荐任何证券的
买卖或持有。仓库内出现的所有代码、价位、标签均为结构示例或占位,`examples/` 与
`demo/` 中的全部数字为虚构。

**本项目可以自动向券商提交真实订单。** 启用前请自行确认 API 权限、订单类型、
金额上限、账户结算规则,以及自动化失败时的兜底。**由此产生的任何盈亏均由使用者自行承担。**

完整声明见 [DISCLAIMER.md](DISCLAIMER.md)。

## License

[MIT](LICENSE)
