# 安装与配置

## 前置要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | **3.11+** | 脚本层。用 `tomllib` 读配置,**无任何第三方依赖** |
| 一个 AI 编码 CLI | — | Claude Code 有原生 skill 路由;其他 CLI 靠 `AGENTS.md` + 自然语言指定 mode(见 §4) |
| 券商 MCP server | — | 实时行情、持仓、下单。可选:不接也能用日报与复盘部分 |
| macOS | — | 仅 `doctor` 的防休眠检查依赖 macOS,其他跨平台 |

```bash
python3 --version   # 需要 >= 3.11
```

---

## 1. 克隆与自检

```bash
git clone https://github.com/jjMurphy1012/meigu-ops.git
cd meigu-ops
make doctor
```

首次运行 `doctor` 一定会报 `config/profile.toml` 缺失——这是预期的,下一步就是配它。

## 2. 配置

```bash
cp config/profile.example.toml  config/profile.toml
cp config/watchlist.example.toml config/watchlist.toml
```

**这两个文件已被 gitignore,永远不会进仓。**

### `config/profile.toml` 必填项

```toml
[account]
id = "你的下单子账户号"      # 主账户只读,不填这里
display_last4 = "后4位"      # 汇报和日报里只露这个
type = "cash"                # cash 的卖出资金 T+1 结算,会影响当日可用买力

[trade]
size_std = 50                # 单笔标准尺寸
size_max = 80                # 极强信号的单笔上限

[cash]
bp_target_pct = 20           # 常态目标:buying power 占总值 < 这个 %
```

其余参数都有合理默认值,可以先跑起来再调。每项的含义都写在样例文件的注释里。

> ⚠️ **`config/profile.example.toml` 里的账户号是占位值 `000000000`。**
> 脚本会检测到你在用样例配置并拒绝执行下单相关流程。

### `config/watchlist.toml`

按你自己的主线改 `[[groups]]`。日报 §12 和盘前候选扫描都从这里读——
**换主线只改这个文件,纪律手册一行都不用动。**

## 3. 验证

```bash
make doctor          # 应该只剩防休眠类的提醒
make test            # 217 个测试应全绿
make trading-day     # 今天是不是交易日
make report          # 生成 reports/{今天}.md 骨架
```

---

## 4. AI CLI 接入

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

## 5. 券商 MCP 与下单权限(可选)

只有要自动下单才需要这一节。**下单前请先读 `DISCLAIMER.md`。**

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

## 6. 日常使用

### 下单授权(默认关闭)

`config/profile.toml` 的 `[execution]` 默认 `enabled = false` —— **clone 这个仓库不会
继承任何人的下单授权**。要下真单,依次放开:

```toml
[execution]
enabled = true              # ① 总开关
dry_run = false             # ② 关掉演练模式
require_confirmation = true  # ③ 建议先保留逐笔确认,跑顺了再考虑关
max_order_usd = 80          # 硬上限由 preflight 强制
max_daily_usd = 200
```

`touch data/HALTED` 可立即停手(preflight 会一律 DENY),删掉该文件恢复。

**每笔下单必经 preflight:**

```bash
make preflight-example > /tmp/order.json   # 看字段模板
# 填好后
python3 scripts/preflight.py --order-file /tmp/order.json
```

返回 `DENY` 就是不许下 —— 不得绕过。详见 `modes/trade.md` Step 1。

---

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

## 7. 常见问题

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
