# 架构

## 设计目标

一个人管一个小额美股账户,最大的敌人不是选股能力,而是**判断质量随记忆和情绪漂移**。
meigu-ops 要解决的就是这个:把纪律固化成文件,把机械活交给脚本,
让每天的决策质量不依赖"今天状态好不好"。

## 四条设计原则

> v5.1.0 的教训见 `CHANGELOG.md`:原则三(机械活下沉到脚本)在 v5.0.0 里
> 只做到了一半 —— 最有后果的下单闸门被留成了散文。

### 一、薄路由 + 惰性加载

```
.claude/skills/meigu-ops/SKILL.md   ~150 行,只有路由表和菜单
        │
        ├─ 先读 AGENTS.md(契约)+ modes/_mechanics.md(平台机制,公开)
        │  再读用户策略层:config/rules.toml + modes/_strategy.md(均 gitignore)
        │
        └─ 再按需读一个 → modes/{premarket|check|trade|daily|journal|review|stats}.md
```

**为什么:** 前身是一个 41KB 的单体技能文件,每次会话全量进上下文——
写日报时也要加载下单陷阱,查统计时也要加载财报解读。
现在写日报只读 `daily.md`,盘中决策只读 `check.md`。

**代价:** 活文档维护变成多文件。对策是 `AGENTS.md` §6 明确"改哪个 mode 就更新哪个",
版本日志统一收在 `CHANGELOG.md`——这样版本历史也不再占用每次会话的上下文。

### 二、系统层 / 用户层硬分离

```
   ┌─────────────── 可开源 ───────────────┐   ┌──── 永不进仓 ────┐
   AGENTS.md  modes/  templates/  scripts/     config/profile.toml
   docs/  examples/  tests/  .github/          data/  reports/
   config/*.example.toml                       .claude/settings.local.json
   └──────────────────┬───────────────────┘   └────────┬─────────┘
                      │                                │
              .gitignore 强制                  make check-privacy 兜底
                      │                                │
                      └────── CI: no-user-data.yml ────┘
```

判断标准是一个问题:**这是"怎么做"还是"做了什么"?**
详见 `docs/DATA_CONTRACT.md`。

这条约束有两个意外的好处:
① 为了不泄露账户规模,所有绝对金额被抽成 `config` 参数或百分比 → **纪律与账户规模解耦**;
② 为了不泄露策略,标签词表与规则被抽成配置 → **框架与具体策略解耦**。
仓库因此从『某个人的交易纪律』变成『把你自己的纪律外化成系统的工具』。

### 三、机械活下沉到脚本,LLM 只做判断

| 工作 | 归属 | 理由 |
|---|---|---|
| 交易日 / 半日市 / 观察日顺延 | 脚本 | 规则确定,人算会错,错一次毁一天 |
| 日报编号与文件名 | 脚本 | 同上 |
| 日志结构校验(标题/倒序/孤儿段落/行数) | 脚本 | 三个真实 bug 都是"以为改对了" |
| 台账字段校验与 FIFO 配对 | 脚本 | 缺一笔就全错,必须报行号 |
| 隐私检查 | 脚本 | 人会忘,而这个错误不可逆 |
| **下单闸门**(意图时效、报价时间戳、减仓占比、集中度、单日上限、ref_id 去重) | **脚本** | v5.0.0 把这些留成了散文,是本项目最大的内部矛盾 —— **最有后果的机械检查最不该靠自觉**(v5.1.0 修正) |
| `.gitignore` 是否真的覆盖了用户层 | 脚本 | 直接问 git,不做正则推断(v5.0.0 因后缀 yml/toml 不匹配而失守) |
| 判断买/卖/不动 | LLM | 需要综合行情、新闻、纪律 |
| 日志摘要的语义压缩 | LLM | 脚本决定"压哪些、压到几行",文字由 LLM 写 |
| 规则有效性审计 | LLM | 脚本给数字,LLM 判断规则该保留还是删 |
| **TUI 的每一行渲染** | **脚本(纯函数)** | 渲染写成 `render_*(data,w,h,state) -> list[str]`,curses 只贴字符 → 整个 UI 可被单元测试。**错位的表格会被人当成数据读错**,所以有"每行宽度精确等于 width"的不变量测试(它当场抓出了 `pad()` 截断双宽字符时少 1 列的 bug) |

**脚本的价值不是省时间,是让同一个错误不可能犯第二次。**

### 四、每条规则都带证据

每条规则在 `config/rules.toml` 里都有 `evidence` 字段:哪次交易让你确立它、结果如何。
没有证据的规则半年后没人知道该不该信。`modes/review.md` 的审计机制定期拿台账数据
检验每一条——**从未被数据支持过的规则是包袱,不是资产,要删掉。**

审计器会在**样本不足时拒绝下结论** —— 拿两三笔盈利去『验证』一条你本来就相信的规则,
是这类机制最容易退化的方式。

---

## 目录结构

```
meigu-ops/
├── AGENTS.md ◄─────────────── 唯一真相源(铁律 / 数据契约 / 不可信内容 / 输出约定)
├── CLAUDE.md                  一行 @AGENTS.md,其他 CLI 照此建转发文件
├── README.md README.en.md DISCLAIMER.md LICENSE VERSION CHANGELOG.md Makefile
│
├── .claude/skills/meigu-ops/
│   └── SKILL.md ◄──────────── 薄路由:模式表 + 意图推断 + 每日节奏 + 脚本清单
│
├── modes/
│   ├── _mechanics.md ◄─────── 平台机制与执行陷阱(公开,不含策略)
│   ├── _strategy.example.md    策略模板(只有问题,没有答案)
│   ├── _strategy.md ░私有░     你的策略(gitignore)
│   ├── premarket.md           盘前:定方案、候选清单、防守预案
│   ├── check.md               盘中:三问过滤 → 买/卖/不动
│   ├── trade.md               下单:preflight → review → place → 汇报 → 记台账
│   ├── daily.md               收盘日报:15 节的判断要点
│   ├── journal.md             尾盘:日志 + 台账 + 分层压缩
│   ├── review.md              复盘:规则有效性审计 + 三类错误分类
│   └── stats.md               统计口径与解读框架
│
├── demo/          demo.tape(vhs 录制脚本)· order-oversized-trim.json(演示固件)
├── templates/     daily-report.md  journal-entry.md  review.md
├── config/        profile.example.toml  watchlist.example.toml
│
├── scripts/       (Python 3.11+,零第三方依赖)
│   ├── meigu_lib.py        路径 / 配置 / 交易日历 / 台账解析 / 标签词表
│   ├── trading_day.py      交易日历查询
│   ├── new_report.py       日报骨架生成
│   ├── journal_compress.py 日志结构校验 + 压缩规划
│   ├── stats.py            FIFO 实现盈亏 / 胜率 / 标签绩效 / 检查点分布
│   ├── preflight.py        ★ 下单前置确定性闸门 → ALLOW / DRY_RUN / DENY
│   ├── dashboard.py        只读 TUI(curses):组合 / 台账 / 纪律
│   ├── snapshot.py         持仓行情快照归档
│   ├── doctor.py           环境自检
│   └── check_privacy.py    数据分层契约的机器兜底
│
├── tests/         217 个测试:日历边界 / 三个真实日志 bug / gitignore 实际行为 / preflight 每道闸门
├── docs/          SETUP · ARCHITECTURE · DATA_CONTRACT
├── examples/      sample-report.md  sample-trades.tsv(数据全虚构)
├── .github/workflows/  test.yml  no-user-data.yml
│
├── data/     ░░░ 用户层 ░░░  trades.tsv journal.md lessons.md snapshots/ _archive/
└── reports/  ░░░ 用户层 ░░░  YYYY-MM-DD.md
```

---

## 数据流

```
                    ┌──────────── 券商 MCP + WebSearch ────────────┐
                    │  行情 / 持仓 / 财报日历 / 新闻                │
                    └───────────────────┬──────────────────────────┘
                                        ▼
  09:12  premarket ──► 今日方案:候选清单 + 触发位 + 防守预案 + 今日不做什么
                                        │
                                        ▼
  10:33  check ──┐
  13:03  check ──┼──► 三问过滤 ──► 不动(逐个点名理由)
  15:37  check ──┘         │
                           └──► trade ──► preflight(ALLOW/DRY_RUN/DENY)→ review → place
                                              │
                                              ├──► 事后完整汇报(给用户)
                                              └──► data/trades.tsv ◄── 唯一真相源
                                        │
                                        ▼
  16:06  journal ──► data/journal.md(五块结构)+ 补齐台账 + 分层压缩
                                        │
                          ┌─────────────┴─────────────┐
                          ▼                           ▼
                   daily ──► reports/*.md      stats ──► FIFO / 标签绩效
                          │                           │
                          └────────────┬──────────────┘
                                       ▼
                    review ──► 规则有效性审计 + 三类错误分类
                                       │
                          ┌────────────┴────────────┐
                          ▼                         ▼
                   data/lessons.md            modes/*.md + CHANGELOG
                   (证据与过程)                 (提炼出的规则)
```

**闭环的关键在最后一步:** 复盘不只是记录,它必须能**修改甚至删除**纪律手册里的规则。
没有这一步,手册只会单调膨胀,最后变成没人读的教条。

---

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 配置格式 | **TOML** | Python 3.11+ 的 `tomllib` 是标准库 → 脚本零依赖。YAML 需要 PyYAML |
| 台账格式 | **TSV** | 制表符不会和股票代码、中文理由冲突;`git diff` 友好;标准库可解析 |
| 脚本语言 | **Python 3.11+** | macOS 自带,标准库够用,`zoneinfo` 处理 ET 时区 |
| 盈亏口径 | **FIFO** | 与多数券商的税务口径一致;分数股场景下比加权平均更可追溯 |
| 时区 | **一律显式 ET** | 本机时钟会漂移(2026-07-13 从 ET 漂到 MDT,全部 cron 错位 2 小时) |
| 日志压缩 | **脚本判断 + LLM 写** | 结构校验能自动化,语义压缩不能 |
| 下单授权 | **本地配置,默认关闭** | 授权是本地事实,不是项目属性 —— 公开仓不能让 clone 者继承别人的授权 |
| 版本日志 | **独立 CHANGELOG.md** | 从技能文件里搬出来,不再占用每次会话的上下文 |
| 演示 gif | **vhs 脚本化录制** | 手动录屏改一次界面就得重录,且会录进本机用户名。`demo/demo.tape` 可复现、可进 CI |
| 演示数据 | **只用 `examples/` 与 `demo/` 固件** | gif 是像素,进公开仓后无法被 `check_privacy.py` 审计 → 必须在**生成环节**把输入限死(`tests/test_demo_tape.py`) |

## 从 v4.12 单体技能迁移过来的变化

| v4.12(单体 `meigu-trade` 技能) | v5.0(`meigu-ops`) |
|---|---|
| 41KB 单文件,每次全量加载 | 薄路由 + 8 个惰性 mode |
| §0-§7 纪律 + §9 更新日志混在一起 | 纪律进 `modes/`,日志进 `CHANGELOG.md` |
| 账户号硬编码 7 处 | 只在 gitignore 的 `config/profile.toml` |
| 单笔尺寸写死 `$30-60` | `config` 参数 `trade.size_std` / `size_max` |
| 关注股硬编码在模板里 | `config/watchlist.toml` |
| 日志压缩/日历/统计全靠 LLM 手做 | 9 个脚本 + 217 个测试 |
| 两份内容重复的日报模板 | 一份 `templates/daily-report.md` + `modes/daily.md` 判断要点 |
| 无版本控制、无隐私边界 | git + `.gitignore` + `check_privacy` + CI |
