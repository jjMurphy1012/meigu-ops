PY := python3
S  := scripts

.DEFAULT_GOAL := help

.PHONY: help setup setup-checklist start-drill doctor trading-day calendar report journal-check \
        journal-compress stats \
        preflight preflight-example rules-check dashboard dashboard-demo demo snapshot \
        check-privacy test lint clean

help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## ★ 首次接入:看当前处于哪一步、下一步做什么
	@$(PY) $(S)/setup.py

setup-checklist: ## 接入各步的验收项清单
	@$(PY) $(S)/setup.py --checklist

start-drill: ## 开一次 dry-run 演练,打印 run id(preflight 用它写证据)
	@$(PY) $(S)/setup.py --start-drill

doctor: ## 环境自检(盘前第一件事)
	@$(PY) $(S)/doctor.py

trading-day: ## 今天是不是交易日 / 上下一个交易日
	@$(PY) $(S)/trading_day.py

calendar: ## 本年度全部休市日与半日市
	@$(PY) $(S)/trading_day.py --year $$(date +%Y)

report: ## 生成当日收盘日报骨架到 reports/
	@$(PY) $(S)/new_report.py

journal-check: ## 校验 data/journal.md 结构(标题/倒序/孤儿段落/行数)
	@$(PY) $(S)/journal_compress.py --check

journal-compress: ## 校验 + 输出分层压缩计划
	@$(PY) $(S)/journal_compress.py

stats: ## 台账统计(FIFO 实现盈亏 / 胜率 / 标签绩效)
	@$(PY) $(S)/stats.py

preflight: ## 下单前置检查(从 stdin 读订单 JSON,返回 ALLOW/DRY_RUN/DENY)
	@$(PY) $(S)/preflight.py --stdin

rules-check: ## 校验 config/rules.toml(id/标签/闸门/散文同步)
	@$(PY) $(S)/rules.py

# 自我进化的写回:证据免批准,改状态需 --approved
#   python3 scripts/rules.py --record-evidence <id> "…"
#   python3 scripts/rules.py --set-status <id> supported --approved --note "…"
#   python3 scripts/rules.py --add-rule <id> "一句能被证伪的话"

preflight-example: ## 打印订单 JSON 模板
	@$(PY) $(S)/preflight.py --example

dashboard: ## 只读仪表盘 TUI(组合 / 台账 / 纪律)
	@$(PY) $(S)/dashboard.py

dashboard-demo: ## 同上,但用 examples/ 的虚构数据(演示与录制用)
	@$(PY) $(S)/dashboard.py --demo

demo: ## 用 vhs 重新录制 docs/demo.gif(需要 brew install vhs)
	@command -v vhs >/dev/null || { echo "需要 vhs:brew install vhs"; exit 1; }
	@$(PY) -m unittest -q tests.test_demo_tape 2>&1 | tail -1
	@vhs demo/demo.tape
	@ls -lh docs/demo.gif

check-privacy: ## 提交前隐私检查(CI 同款)
	@$(PY) $(S)/check_privacy.py

test: ## 跑全部单元测试
	@PYTHONPATH=$(S) $(PY) -m unittest discover -s tests -v

lint: ## 编译期语法检查(无第三方依赖)
	@$(PY) -m compileall -q $(S) tests && echo "✅ 语法检查通过"

clean: ## 清理 __pycache__
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ 已清理"
