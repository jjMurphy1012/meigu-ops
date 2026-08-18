# 安装与配置

## 前置要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | **3.11+** | 脚本层。用 `tomllib` 读配置,**无任何第三方依赖** |
| 一个 AI 编码 CLI | — | Claude Code 有原生 skill 路由;其他 CLI 靠 `AGENTS.md` + 自然语言指定 mode(见 §4) |
| 券商 MCP server | — | 实时行情、持仓、下单。**接入是 setup 第 2 步**;只想看效果可走 Demo 分支 |
| macOS | — | 仅 `doctor` 的防休眠检查依赖 macOS,其他跨平台 |

```bash
python3 --version   # 需要 >= 3.11
```

---

## 1. 克隆,然后交给状态机

```bash
git clone https://github.com/jjMurphy1012/meigu-ops.git
cd meigu-ops
make setup
```

`make setup` 会告诉你当前处于哪一步、下一步该做什么。**下面各节是它每一步的展开,
不是需要你手动照抄的顺序** —— 以 `make setup` 的输出为准。

### 顺序原则:连接要早、只读;授权要晚、单独

```
UNINITIALIZED → MCP_CONNECTED_READONLY → PROFILE_READY
              → STRATEGY_READY → AUTOMATION_READY → LIVE_AUTHORIZED
```

这个项目的核心是"AI + 券商 MCP 自动交易"。**所以券商连接排在第二步,而不是最后一步**:
若把它放到最后,你会配置半天才发现账户、权限或 MCP 根本不可用 —— 而那时 MCP 的问题
和项目配置的问题已经混在一起,分不清是谁的错。

但**连接成功不等于获得真钱执行权限**。第二步是纯只读的:读账户、持仓、买力、报价,
`place_equity_order` 一次都不会被调用。真钱授权是最后一步,单独做。

**状态是算出来的,不是记下来的。** 改了账户号、删了规则文件、规则文件读不出来,
`make setup` 会自动落回那一步。

```bash
make setup-checklist   # 看每一步的验收项
```

### 只想看效果?不必授权券商

```bash
make dashboard-demo
python3 scripts/stats.py --demo
```

用虚构固件跑完整工作流,不需要任何账户。

---

## 2. 券商 MCP 只读验证(状态机第 2 步)

装好并登录 Robinhood MCP 后,跑 `/meigu-ops setup`,由 AI 执行只读调用:
读账户列表 → 在其中定位你的子账户(且账户号唯一)→ 读持仓 → 读 buying power
→ 取**带时间戳**的报价 → 确认 `review_equity_order` 可调用。

**验收标准是真的读到数据,不是"检测到 MCP 配置文件"。** 九项逐条校验,缺一不可;
读到的账户号与 `config/profile.toml` 不一致会**直接拒绝**,不会替你选一个;
验证期间执行开关若是开着的,也会被拒绝 —— "只读"必须名副其实。

## 3. 配置(状态机第 3 步)

```bash
cp config/profile.example.toml  config/profile.toml
cp config/watchlist.example.toml config/watchlist.toml
```

**这两个文件已被 gitignore,永远不会进仓。**

### `config/profile.toml` 必填项

```toml
[account]
id = "刚刚验证过的下单子账户号"   # 主账户只读,不填这里
display_last4 = "后4位"          # 汇报和日报里只露这个
type = "cash"                    # cash 的卖出资金 T+1 结算,会影响当日可用买力

[execution]                      # 四条硬上限:不随证据强度变化,任何规则都绕不过
max_order_usd = 80               # 单笔
max_daily_usd = 200              # 单日**买入**累计(卖出不受此限)
max_orders_per_day = 6
kill_switch_file = "data/HALTED"
```

按"单日全损也能承受"的量级来定这四个数。此时执行开关仍全部关闭 ——
**这一步只是配置,不是授权。**

> ⚠️ 样例里的账户号是占位值。状态机检测到占位值就不会放行下一步。

### `config/watchlist.toml`

按你自己的主线改 `[[groups]]`。日报与盘前候选扫描都从这里读——
**换主线只改这个文件,纪律手册一行都不用动。**

## 4. 你自己的策略(状态机第 4 步)

```bash
cp config/reason-tags.example.toml config/reason-tags.toml
cp config/rules.example.toml        config/rules.toml
cp modes/_strategy.example.md       modes/_strategy.md
```

**这是最花时间、也是唯一不能让 AI 替你做的一步。**
`modes/_strategy.example.md` 里全是问题,没有答案 —— 本仓库不提供任何交易策略。

没有市场判断类规则时,系统只能跑分析与 dry-run,不能下真单。**这是设计如此。**

```bash
make rules-check     # 格式、标签引用、闸门引用
```

## 5. 演练与验证(状态机第 5 步)

```bash
make doctor          # 应该只剩防休眠类的提醒
make test            # 334 个测试应全绿
make trading-day     # 今天是不是交易日
make report          # 生成 reports/{今天}.md 骨架
```

```bash
python3 scripts/setup.py --start-drill    # 拿 run id
```

确认 `dry_run = true`,然后完整跑一遍:盘前 → 盘中 → preflight → 模拟下单
(走完 `review`,**不 `place`**)→ 尾盘日志 → 复盘。
跑 preflight 时在订单里带上 `"drill_run_id"`,判定会被写进 `data/drill-runs.jsonl` ——
`--record-drill` 只认这些证据,不认自报的布尔值。

演练的意义是把"配置对不对"和"链路通不通"分开验证。**链路没跑通就开真钱,
出问题时你分不清是策略错了还是管道漏了。**

---

## 6. AI CLI 接入

### Claude Code

技能已在 `.claude/skills/meigu-ops/SKILL.md`,克隆后自动发现:

```
/meigu-ops              # 显示菜单
/meigu-ops premarket    # 盘前分析
/meigu-ops daily        # 收盘日报
```

也可以直接说人话:"AAAA 现在能加吗"、"写今天的日报"、"这周复盘一下"。

### 其他 CLI

`AGENTS.md` 是唯一共享上下文。多数 CLI 会自动读 `AGENTS.md`;若你的 CLI 用别的
文件名(`CODEX.md` / `GEMINI.md` / `.cursor/rules`),照 `CLAUDE.md` 的样子建一个
只含 `@AGENTS.md` 的转发文件即可,**不要复制内容**——复制就会分叉。

若 CLI 不支持斜杠命令,直接用自然语言指定模式:

```
按 modes/premarket.md 做今天的盘前分析
按 modes/daily.md 写收盘日报
```

---

## 7. 会话与机器层面的前置条件

券商 MCP 只读验证能过,不代表无人值守跑得起来。这一节的两件事都曾真实造成损失。

### 权限白名单——这一层不通会冻结整个会话

策略层授权 ≠ 工具层放行。**未进白名单的 MCP 工具每次调用都会弹确认框;
你不在场时会无限等待,整个会话冻结、后续所有检查点被阻塞。**
2026-07-09 曾因此损失一整天(当时误判为"MCP 挂起 6 小时")。

`.claude/settings.local.json`(此文件已 gitignore):

```json
{
  "permissions": {
    "allow": [
      "mcp__robinhood-trading__review_equity_order",
      "mcp__robinhood-trading__place_equity_order",
      "mcp__robinhood-trading__cancel_equity_order",
      "mcp__robinhood-trading__get_equity_orders"
    ]
  }
}
```

`make doctor` 会检查这四件套是否齐全。

### 防休眠(macOS)

交易时段机器必须真正醒着。**AC 电源不够,合盖照睡。**
同一笔减仓曾连续两天在 `review` 与 `place` 之间被休眠截杀。

免 sudo 方案——常驻 `caffeinate`:

```bash
cat > ~/Library/LaunchAgents/com.ustock.keepawake.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ustock.keepawake</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-is</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
PLIST

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ustock.keepawake.plist
pgrep -x caffeinate && echo "✅ caffeinate 已常驻"
```

**残余缺口:`caffeinate` 挡不住物理合盖。** 彻底根治需要:

```bash
sudo pmset -a disablesleep 1
```

或者交易时段保持开盖。`make doctor` 会同时检查这三项。

---

## 8. 授权真钱执行(状态机第 6 步)

**下单前请先读 `DISCLAIMER.md`。**

`[execution]` 默认 `enabled = false` —— **clone 这个仓库不会继承任何人的下单授权。**
前五步全部通过后,用脚本授权,不要手改配置:

```bash
# 先用 guarded:仓位统一压到最低档,不管规则状态多好
python3 scripts/setup.py --authorize-live guarded --approved

# 跑一段、确认健康之后,再考虑放开仓位缩放
python3 scripts/setup.py --authorize-live autonomous --approved
```

不带 `--approved` 会直接退出;前五步没全过也会被拒绝。写入会**回读校验** ——
值没真的落盘就不会报成功。

而且这不是一次性检查:**每一笔真单进 `preflight` 时都会重新验一遍六步状态**。
`[execution]` 是个可以手改的 TOML,只在授权那一刻检查是拦不住手改和旧配置的。

**"开不开真钱"与"放不放开仓位"是两个独立决定。** 不要一次做完。

`touch data/HALTED` 可立即停手(preflight 会一律 DENY),删掉该文件恢复。

**每笔下单必经 preflight:**

```bash
make preflight-example > /tmp/order.json   # 看字段模板
# 填好后
python3 scripts/preflight.py --order-file /tmp/order.json
```

返回 `DENY` 就是不许下 —— 不得绕过。详见 `modes/trade.md` Step 1。

---

## 9. 日常使用

### 仪表盘

```bash
make dashboard         # 读 data/(真实数据)
make dashboard-demo    # 读 examples/(虚构数据,演示用)
```

三个标签页:**组合**(占比条 / BP vs 目标 / 集中度)、**台账**(可排序,Enter 看 FIFO 配对)、
**纪律**(标签绩效对比 → 核心纪律是否被数据支持)。只读,不下单也不改文件。

组合页需要快照里有 `normalized` 块(见 `docs/DATA_CONTRACT.md` §4);
没有快照时该页会明确说明,不会静默显示 0。

### 重录演示 gif

```bash
brew install vhs
make demo              # → docs/demo.gif
```

录制脚本是 `demo/demo.tape`,只允许跑 demo 模式与 `demo/` 固件 ——
`tests/test_demo_tape.py` 强制这条(gif 是像素,进公开仓后无法被隐私扫描审计)。

---

```bash
make doctor            # 盘前第一件事
make preflight         # 下单前置检查(必经)
make dashboard         # 只读仪表盘
make report            # 生成日报骨架
make journal-check     # 写日志前后校验结构
make journal-compress  # 拿到分层压缩计划
make stats             # 台账绩效统计
make check-privacy     # 提交前
make test              # 改了脚本之后
make help              # 列出全部命令
```

## 10. 常见问题

**Q:`doctor` 报 `config/profile.toml` 缺失并非零退出,是坏了吗?**
A:不是。全新 clone 尚未配置时这是**预期行为** —— doctor 的职责就是在你开始工作前
挡住"配置没就位"。复制配置后即转为通过。

**Q:`doctor` 说 `config/profile.toml` 缺失,但我不打算下单。**
A:仍然需要复制一份——尺寸参数、现金目标、日志行数上限都从它读。账户号留占位值即可。

**Q:`make journal-check` 报"超过上限"。**
A:这是设计如此。跑 `make journal-compress` 拿计划,按 `modes/journal.md` Step 4 压缩。
行数上限在 `config/profile.toml` 的 `journal.max_lines`。

**Q:`make stats` 报台账格式错误。**
A:按报出的行号修 `data/trades.tsv`。**不要删行**——缺一笔会让整段 FIFO 配对出错。

**Q:交易日历准不准?**
A:观察日顺延、耶稣受难日、半日市都按 NYSE 规则计算,有 40+ 个边界测试覆盖。
但**不含国葬、极端天气等临时休市**——那类无法预测。

**Q:`check-privacy` 报了一个我认为是误报的东西。**
A:确认真的是占位/测试值后,在**那一行**行尾加 `# privacy-allow`(或该语言的注释形式)。
**不要用整文件白名单** —— 它会连同该文件未来新增的每一行一起豁免,
2026-08-18 真实账户号泄漏到公开仓就是这么发生的。
更不要为了让检查通过就放宽正则 —— 这是数据分层契约的最后一道防线。
