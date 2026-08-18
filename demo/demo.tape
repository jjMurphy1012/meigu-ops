# meigu-ops 演示录制脚本(vhs)
#
#   brew install vhs
#   make demo            # → docs/demo.gif
#
# 叙事顺序 = 新用户 clone 之后的真实顺序:
#   接入状态机 → 各步验收项 → 仪表盘三页 → 下单闸门真的拦住一笔
#
# ⚠️ 隐私约束:本脚本**只允许**跑 demo 模式与 demo/ 下的固件。
#    任何会读 data/ 或 config/profile.toml 的命令都不得出现在这里 ——
#    gif 会被提交进公开仓,里面出现的每一个数字都必须是虚构的。
#    tests/test_demo_tape.py 会强制这条约束。

Output docs/demo.gif

Set Shell        "bash"
Set FontSize     14
Set Width        1180
Set Height       660
Set Padding      14
Set Framerate    12
Set TypingSpeed  32ms
Set Theme        "Catppuccin Mocha"

Hide
Type "export PS1='$ ' && clear"
Enter
Show

Sleep 700ms

# ══════════════════════════════════════════════════════════
# 场景一:clone 之后的第一件事 —— 接入状态机
#   连接要早、只读;授权要晚、单独
# ══════════════════════════════════════════════════════════
Type "# clone 之后的第一件事:make setup 会告诉你现在在哪一步"
Enter
Sleep 1200ms

Type "python3 scripts/setup.py --demo"
Sleep 300ms
Enter
Sleep 6000ms

Type "clear"
Enter
Sleep 300ms

# ══════════════════════════════════════════════════════════
# 场景二:验收标准是「真的读到数据」
# ══════════════════════════════════════════════════════════
Type "# 第 2 步的验收项 —— 九项逐条核对,全程不调用 place_equity_order"
Enter
Sleep 1200ms

Type "python3 scripts/setup.py --checklist"
Sleep 300ms
Enter
Sleep 5200ms

Type "clear"
Enter
Sleep 300ms

# ══════════════════════════════════════════════════════════
# 场景三:仪表盘 · 组合页
# ══════════════════════════════════════════════════════════
Type "make dashboard-demo"
Sleep 400ms
Enter
Sleep 2400ms

Down
Sleep 600ms
Down
Sleep 900ms

# ── 台账页:每笔带理由标签;Enter 看 FIFO 配对 ──
Tab
Sleep 2000ms

Type "s"
Sleep 500ms
Type "s"
Sleep 1500ms

Down
Sleep 800ms
Enter
Sleep 3600ms
Escape
Sleep 600ms

# ── 纪律页:这个项目真正独特的地方 ──
#    "我这条纪律是否真的被数据支持" 被回答
Tab
Sleep 3400ms

Down
Sleep 500ms
Down
Sleep 1500ms

Type "q"
Sleep 800ms

# ══════════════════════════════════════════════════════════
# 场景四:下单闸门 —— 重放 2026-07-29 那笔真实事故
#   意图"部分减仓",但仓位已缩水,实际会卖掉 91%
# ══════════════════════════════════════════════════════════
Type "clear"
Enter
Sleep 400ms

Type "# 每笔下单必经闸门。重放一次真实事故:意图是「部分减仓」,"
Enter
Sleep 900ms
Type "# 但这个仓位已经大跌到只剩一个标准尺寸"
Enter
Sleep 1200ms

Type "python3 scripts/preflight.py --order-file demo/order-oversized-trim.json --profile demo/profile-demo.toml --tags examples/sample-reason-tags.toml --now-et '2026-08-18 13:05'"
Sleep 300ms
Enter
Sleep 5500ms
