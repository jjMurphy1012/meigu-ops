# meigu-ops 演示录制脚本(vhs)
#
#   brew install vhs
#   make demo            # → docs/demo.gif
#
# ⚠️ 隐私约束:本脚本**只允许**跑 demo 模式与 demo/ 下的固件。
#    任何会读 data/ 或 config/profile.toml 的命令都不得出现在这里 ——
#    gif 会被提交进公开仓,里面出现的每一个数字都必须是虚构的。
#    tests/test_demo_tape.py 会强制这条约束。

Output docs/demo.gif

Set Shell        "bash"
Set FontSize     14
Set Width        1180
Set Height       624
Set Padding      14
Set Framerate    12
Set TypingSpeed  35ms
Set Theme        "Catppuccin Mocha"

# ── 准备:清屏,不录入 cd 之类的噪音 ──
Hide
Type "export PS1='$ ' && clear"
Enter
Show

Sleep 800ms

# ══════════════════════════════════════════════════════════
# 场景一:仪表盘 · 组合页
#   持仓占比条 / BP vs 目标 / 集中度
# ══════════════════════════════════════════════════════════
Type "make dashboard-demo"
Sleep 400ms
Enter
Sleep 2500ms

Down
Sleep 600ms
Down
Sleep 900ms

# ══════════════════════════════════════════════════════════
# 场景二:台账页
#   每笔带理由标签,可排序;Enter 看 FIFO 配对
# ══════════════════════════════════════════════════════════
Tab
Sleep 2200ms

# 按金额排序
Type "s"
Sleep 500ms
Type "s"
Sleep 1600ms

# 落到一笔**卖出**上 —— 只有卖出行才有 FIFO 配对与实现盈亏可看
Down
Sleep 900ms

# 展开:先买的先出、持有天数、实现盈亏
Enter
Sleep 3800ms
Escape
Sleep 700ms

# ══════════════════════════════════════════════════════════
# 场景三:纪律页 —— 这个项目真正独特的地方
#   "我这条纪律是否真的被数据支持" 被回答
# ══════════════════════════════════════════════════════════
Tab
Sleep 3500ms

Down
Sleep 500ms
Down
Sleep 1500ms

Type "q"
Sleep 900ms

# ══════════════════════════════════════════════════════════
# 场景四:下单闸门 —— 重放 2026-07-29 那笔真实事故
#   意图"部分减仓",但仓位已缩水,实际会卖掉 91%
# ══════════════════════════════════════════════════════════
Type "clear"
Enter
Sleep 400ms

Type "# 重放一次真实事故:意图是「部分减仓」,但这个仓位已经大跌到只剩一个标准尺寸"
Enter
Sleep 1400ms

# 一行写完 —— vhs 会把 "\\" 打成字面双反斜杠,bash 的续行会失效
Type "python3 scripts/preflight.py --order-file demo/order-oversized-trim.json --tags examples/sample-reason-tags.toml --now-et '2026-08-18 13:05'"
Sleep 300ms
Enter
Sleep 5000ms
