# 变更日志

所有对纪律手册和系统的改动都记在这里。维护规则见 `AGENTS.md` §6。

版本号约定:小修 `+0.0.1` · 规则调整 `+0.1.0` · 策略姿态级改动 `+1.0.0`。

---

## v5.3.0(2026-08-18)· 策略与框架彻底分离

复审指出:底层已经通用化,但**上层 AI 工作流仍在执行原作者的个人策略** ——
`_shared.md` 里 10 个理由标签、现金目标、准入判定、催化窗口全都还在,
而 preflight 已经改读中立词表。**AI 生成的每一笔都会被自己的闸门拒绝。**
这一版把策略层整体移出仓库。

### 🔴 端到端断裂(已修)

公开 `modes/` 与 `templates/` 里有 6 个文件硬编码了 10 个理由标签,
而脚本读的是 `config/reason-tags.toml`。现在公开层**零个标签字面量**,
全部指向配置;新用户 clone 后必须先定义自己的词表才能记台账。

### 🔴 干净 clone 缺文件(已修)

`.gitignore` 的 `*portfolio*` 兜底通配把 dashboard 的演示固件一起吞了 ——
本地测试全绿,但干净 clone 跑不起来。固件改名 `sample-snapshot.json`,
并对 `examples/**` `demo/**` 显式反向放行。

### 策略层整体私有化

| 文件 | 内容 | 进仓 |
|---|---|---|
| `modes/_mechanics.md` | 平台机制与执行陷阱(去日期/标的/金额) | ✅ |
| `modes/_strategy.example.md` | 只有问题清单,零答案 | ✅ |
| `modes/_strategy.md` | 你的策略 | 🔒 |
| `config/reason-tags.toml` | 你的标签词表 | 🔒 |
| `config/rules.toml` | 你的可检验规则 | 🔒 |

**怎么细分交易理由、相信哪些规则,就是策略本身。** 仓库只给 `*.example.*` 模板。

### 自我进化闭环:`scripts/rules.py`

- 三类规则:`invariant`(不可关闭的流程不变量,无程序把守)/ `enforced`(有闸门把守)/
  `market`(由台账数据裁决)。混淆这三者会让"程序强制"变成一句空话。
- **执行层级与状态正交**:`live` / `observe` / `none`。`hypothesis` 默认只能 `observe`
  —— **零样本的新假设不该拿真钱去试**。preflight 新增「规则作用域」闸门强制这条。
- 审计结论拆成**方向**(supports/refutes/inconclusive)与**强度**
  (insufficient/weak/moderate/strong):<10 事件不下结论,10–19 只给弱支持/弱反驳,
  ≥20 才可**建议**改状态 —— 而任何状态变更都需要用户批准。
- **样本单位是独立决策事件**,不是 FIFO lot:一笔卖单匹配三个买入批次仍算一次决策。
  多标签比较按事件数加权。
- 交叉校验:规则引用的标签必须存在于词表、`enforced_by` 必须命中
  `preflight.GATE_NAMES` —— 写错闸门名会让规则显示「程序强制」但其实无人把守。
- `config/rules.toml` 是执行状态的**唯一真相源**;`_strategy.md` 只提供解释。
  `make rules-check` 检查两者同步。

### 日报骨架也 config 化

指数、板块、主题、技术面标的、分组、风险维度全部来自 `config/watchlist.toml`。
模板里不再出现任何具体行业、主题或个股名 —— 有测试守着这条。

定位随之明确:**通用美股纪律框架**,不是某个方向的研究体系。

### 其他

- `journal` 的活文档回写与新状态机对齐:每日只追加 `evidence`,
  不改状态、不删规则、不动 `CHANGELOG`/`VERSION`(策略变更不属于系统层版本日志)。
- v1.0–v4.12 的历史版本记录移入用户层 `data/history.md`。
- 演示固件彻底中性化(A/B 占位标签),演示里的假设也是 `observe`。
- 测试自带词表,不再依赖本机私有配置 —— 换任何人 clone 结果一致。
- 测试 240 → 244。

---

## v5.2.0(2026-08-18)· 只读仪表盘 TUI + 可复现的演示录制

### 新增 `scripts/dashboard.py` —— curses TUI,零依赖

三个标签页,按"每天真正要看什么"选的,不是照抄别的项目:

| 页 | 内容 |
|---|---|
| **组合** | 持仓占比条 / BP vs `bp_target_pct`(进度条)/ 集中度 vs `max_single_pct`(超限标 `!`) |
| **台账** | `data/trades.tsv` 每一笔,可按日期/标的/金额/标签排序;Enter 展开该笔的 **FIFO 配对**(逐个买入批次 + 持有天数 + 实现盈亏) |
| **纪律** | 按 `reason_tag` 的绩效表 + **核心纪律是否被数据支持**(支持 / 不支持 / 数据不足,只有这三种,不含糊) |

**★ 设计:整个 UI 是纯函数** `render_*(data, width, height, state) -> list[str]`,
curses 只负责贴字符。所以界面能被单元测试完整覆盖 —— 本项目反复吃过的教训是
"防线必须有测试",而**错位的表格会被人当成数据读错**,UI 也是防线。

核心不变量:**每行显示宽度精确等于 width、行数精确等于 height**。
这条测试当场抓出两个真实 bug:
- `pad()` 截断时若跳过的是双宽字符,`out + "…"` 会比 n 少 1 列 → 整张表错位。
- curses 循环画 `lines[:height-1]`,把最后一行的**快捷键栏整行丢掉了**。

仪表盘是**只读**的:不下单,不改任何文件。

### 新增可复现的演示录制

`demo/demo.tape`(vhs)→ `docs/demo.gif`,`make demo` 一条命令重录。
四个场景:组合页 → 台账页排序与 FIFO 详情 → 纪律体检 → **重放 2026-07-29 的真实事故**
(意图"部分减仓"但仓位已缩水,闸门算出会卖掉 90.9% → DENY)。

35 秒 / 1180×624 / **668 KB**(对照:career-ops 的 demo.gif 是 8.3 MB)。

**隐私:gif 是像素,进公开仓后无法被 `check_privacy.py` 审计** ——
所以约束前移到生成环节:`tests/test_demo_tape.py` 强制录制脚本只能跑 demo 模式与
`demo/` 固件,并断言演示固件**真的会触发闸门**(固件若失效,gif 就在说谎)。

同一类思路的第二次应用:一个无法事后检查的产物,必须在生产环节把输入限死。

### 数据契约新增 `normalized`

`data/snapshots/*.json` 的 `checkpoints[*].normalized` 现为仪表盘的**唯一**数据源
(`total_value` / `cash` / `buying_power` / `equity_value` / `positions[]`)。
券商原始字段名不稳定,**脚本不猜** —— 缺这一块时仪表盘明确提示,不静默显示 0。
规范见 `docs/DATA_CONTRACT.md` §4,示例 `examples/sample-portfolio.json`(全虚构)。

### 其他

- `doctor` 的电源检查:`ok` 此前恒为 `True`,导致电池模式显示 ✅ 却附带警告文案,
  且不计入提醒计数 —— 图标与结论自相矛盾,已修。
- `preflight.py --example` 曾被 argparse 的 `required=True` 互斥组拦下,
  `make preflight-example` 根本不能用。**`run()` 全绿不代表 CLI 可用**,已补 5 个 CLI 入口测试。
- CI 冒烟测试增加三个标签页的 `--render` 与 `preflight --example`。
- 测试 142 → 217。

---

## v5.1.0(2026-08-18)· 安全与真实性修正

首版推送后的复审(外部 + 自查)发现的问题。**其中一项是已发生的真实泄漏。**

### 🔴 真实账户号曾泄漏到公开仓(已处置)

`tests/test_privacy.py` 用**真实账户号**当测试固件并被推送到公开仓库,暴露约 70 分钟。
三道本应拦住它的防线被同时关掉:

1. 拿真实账户号当固件(应该用虚构号)
2. 该文件被加进**整文件白名单** `ALLOWLIST_FILES`,扫描器跳过了它
3. CI 的账户号扫描**显式排除了这个文件**

排查中又发现第四重失效:那道 CI 扫描**从一开始就是空转的** ——
`git grep -E` 不支持 `\b` 词边界,正则从未匹配过任何东西,却一直报绿。

**处置:** 固件改虚构号 → 整文件白名单改为**逐行 `privacy-allow` pragma** →
CI 改为「模式选择即自检」(注入已知坏值证明能匹配,匹配不到就 fail)+ 遍历全部 ref →
改用 `printf` 避免 `echo` 展开 `\n` 导致过滤失效 → 删除仓库并用干净历史重建。

**教训(已写进 docs/ARCHITECTURE.md):** 豁免的**粒度**本身就是安全属性。
整文件白名单会豁免该文件未来新增的每一行 —— 它不是"例外",而是一个持续扩大的盲区。
`tests/test_privacy.py::TestPragmaGranularity` 现在锁住这个设计。

### 🔴 `.gitignore` 与实际配置格式不匹配

首版 `.gitignore` 写的是 `config/profile.yml`,而项目实际用 `.toml` ——
**真实配置整整一版没有被保护**。83 个测试里没有一个问过 git
"这个路径真的被忽略了吗"。

**处置:** 改用 `config/profile.*` 通配 + 新增 `tests/test_gitignore.py`,
直接调用 `git check-ignore` 验证**外部系统的实际行为**,而不是验证 `.gitignore` 的文本。

### 🔴 公开仓不得让 clone 者继承下单授权

v5.0.0 的 `AGENTS.md` 把"用户已长期授权自动下单"写成**项目事实**。
那对原作者是真的,但对任何 clone 者等于继承了一份别人的下单授权。

**处置:** 新增 `config/profile.toml` 的 `[execution]` 段,**仓库默认全部关闭**:
`enabled = false` / `dry_run = true` / `require_confirmation = true`,
外加单笔与单日金额上限、笔数上限、意图 TTL、报价时效、`kill_switch_file`。
`AGENTS.md` 与 `modes/trade.md` 改为**从配置读授权**,不再断言。
授权是本地事实,不是项目属性。

### 🟠 下单闸门从散文变成程序:`scripts/preflight.py`

v5.0.0 把"机械活下沉到脚本"写进架构原则,却把**最有后果的**几个机械检查
(尺寸占比、市场时段、意图时效、单日上限)留成了 `modes/trade.md` 里的散文。
这是本项目最大的内部矛盾。

新增 `preflight.py`,返回 `ALLOW` / `DRY_RUN` / `DENY`,覆盖:
紧急停止、`execution.enabled`、账户身份、市场时段与订单类型匹配、
**意图时效**、**报价时间戳熔断**、**减仓占比**(有专门的回归用例)、
残值仓、集中度、买力、现金底线、单日金额与笔数、同标的当日重复、
`ref_id` 格式与去重、理由标签合法性。46 个测试。

**散文约束在状态好的时候有效,程序约束一直有效。**

### 🟠 修正一条错误的「物理事实」:分数股交易限制

v5.0.0 把"分数股只能用市价单 + 正常时段,不支持限价、不支持盘前盘后"写成**绝对物理事实**,
并让多条纪律建立在它之上(盘前不下单、尾盘卖出有时间紧迫性)。

据券商官方文档,实际情况是:分数股**可以**在延长时段(盘前约 7:00–9:30、
盘后约 16:00–19:30 ET)用**限价单**交易,合格性按标的流动性逐一裁定;
24 小时市场则仅限整股。

**处置:** `_shared.md` §0/§1/§3/§5 全部改为**能力探测式**表述
(下单前用 `get_equity_tradability` 核实),并复核了所有依赖它的纪律 ——
尾盘卖出的紧迫性从"绝对"降为"程度问题",但结论(盘中最后窗口更可靠)不变。

**一条错误的"物理事实"会静默地为策略提供正当性,这比缺一个字段危险。**

### 🟡 其他

- CI `no-user-data` 现在扫描全部 git 历史并带自检(见上)。
- README 不再宣称"任何 Agent Skill CLI" —— 只有 Claude Code 有原生 skill 路由,
  其他 CLI 靠 `AGENTS.md` + 自然语言指定 mode。
- `doctor` 新增下单授权状态与紧急停止开关显示;FAQ 说明"全新 clone 未配置时非零退出是预期行为"。
- 测试 83 → 142。

### 已知未做

- **台账扩列(order_id / filled_at / 算术一致性校验 / 期初持仓 lot)** 尚未实施。
  影响:`ref_id` 去重只能尽力而为;已有的期初持仓会让 FIFO 报"卖出无对应买入"。
  `preflight.py` 与 `modes/stats.md` 都已显式声明这个限制,不假装完备。

---

## v5.0.0(2026-08-18)· 架构重构

从单体技能文件重构为可开源的分层系统。**纪律内容全部保留,只改组织方式。**

**新增**

- `AGENTS.md` 作为唯一共享上下文;`CLAUDE.md` 只做 `@AGENTS.md` 转发。
- 薄路由 `.claude/skills/meigu-ops/SKILL.md` + 8 个惰性加载的 `modes/*.md`。
- 系统层 / 用户层硬分离:`.gitignore` + `scripts/check_privacy.py` + CI `no-user-data`。
- 机器可读交易台账 `data/trades.tsv`(10 列 TSV,10 个受控 `reason_tag`)。
- 7 个 Python 脚本(零第三方依赖):交易日历、日报骨架、日志结构校验与压缩规划、
  FIFO 绩效统计、快照归档、环境自检、隐私检查。
- 83 个单元测试,重点覆盖交易日历边界和三个真实发生过的日志结构 bug。
- `modes/review.md` 的**规则有效性审计**机制:定期拿台账数据检验每条纪律,
  从未被数据支持过的规则要删掉——纪律手册第一次有了纠错闭环。
- `docs/`(SETUP / ARCHITECTURE / DATA_CONTRACT)、`examples/`(数据全虚构)、
  `DISCLAIMER.md`、MIT LICENSE。

**变更**

- 技能重命名 `meigu-trade` → `meigu-ops`,调用方式 `/meigu-ops <mode>`。
- 账户号从硬编码 7 处改为只存在于 gitignore 的 `config/profile.toml`。
- 单笔尺寸从写死的绝对美元改为 `config` 参数 `trade.size_std` / `trade.size_max`;
  仓位与现金规则一律用百分比。**副作用是好的:纪律变得与账户规模无关。**
- 关注股清单从模板硬编码改为 `config/watchlist.toml`。
- 两份内容重复的日报模板合并为 `templates/daily-report.md`(结构)
  + `modes/daily.md`(判断要点)。
- v1.0~v4.12 的更新日志从技能文件搬到本文件,不再占用每次会话的上下文。

**已废弃**

- 单体 `.claude/skills/meigu-trade/SKILL.md`(41KB,每次会话全量加载)。
- `auto_trade/` launchd 自动交易系统(2026-07-10 已停用,现归档至 `data/_archive/`)。

---

---

## 更早的版本

v1.0–v4.12 属于单体技能时期,其变更记录含具体交易细节,已移入用户层
(`data/history.md`,不进仓)。**策略演进不属于系统层版本日志** ——
今后规则变更记录在 `config/rules.toml` 各规则的 `evidence` 字段与 `data/lessons.md`。
