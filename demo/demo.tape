# meigu-ops 演示录制脚本(vhs)
#
#   brew install vhs
#   make demo            # → docs/demo.gif
#
# ⚠️ 录制前先 bump VERSION:仪表盘之类的界面会把版本号打在画面上,
#    先录后 bump 会让 gif 里的版本号比 release 落后一版(v5.10.0 时发生过)。
#
# 只演两件事,因为新用户只需要先明白两件事:
#   ① clone 之后我该干什么      → 接入状态机告诉你在第几步
#   ② 它凭什么值得托付真钱      → 闸门真的会拦下一笔坏单
#
# 刻意**不**演示仪表盘、FIFO 配对、规则审计 —— 那些是用起来之后才关心的。
# 演示的目的是让人看懂,不是让人看全。
#
# ⚠️ 隐私约束:本脚本**只允许**跑 demo 模式与 demo/ 下的固件。
#    任何会读 data/ 或真实 config/profile.toml 的命令都不得出现在这里 ——
#    gif 会被提交进公开仓,里面出现的每一个数字都必须是虚构的。
#    tests/test_demo_tape.py 会强制这条约束。

Output docs/demo.gif

Set Shell        "bash"
Set FontSize     15
Set Width        1100
Set Height       600
Set Padding      16
Set Framerate    12
Set TypingSpeed  30ms
Set Theme        "Catppuccin Mocha"

Hide
Type "export PS1='$ ' && clear"
Enter
Show

Sleep 700ms

# ══════════════════════════════════════════════════════════
# 一:clone 之后 —— 我现在在第几步?
# ══════════════════════════════════════════════════════════
Type "# clone 之后只需要记住一条命令:make setup"
Enter
Sleep 1000ms

Type "make setup-demo"
Sleep 400ms
Enter
Sleep 6500ms

Type "clear"
Enter
Sleep 400ms

# ══════════════════════════════════════════════════════════
# 二:每一笔单都要过闸门 —— 这次它拦下了
#    意图"部分减仓",但仓位已缩水到只剩一个标准尺寸
# ══════════════════════════════════════════════════════════
Type "# 到点后 AI 自己分析、自己决定。但下单前必须过闸门:"
Enter
Sleep 1400ms

Type "python3 scripts/preflight.py --order-file demo/order-oversized-trim.json --demo --tags examples/sample-reason-tags.toml --now-et '2026-08-18 13:05'"
Sleep 300ms
Enter
Sleep 6500ms
