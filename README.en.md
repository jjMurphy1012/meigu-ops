<h1 align="center">meigu-ops</h1>

<p align="center">
  <strong>Externalize <em>your own</em> trading discipline into a system that data can audit</strong><br>
  <em>For a self-managed US equities account · Robinhood MCP + Claude Code</em>
</p>

<p align="center">
  <a href="README.md">简体中文</a> | English
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/dependencies-none-2ea44f?style=flat" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/tests-295%20passing-2ea44f?style=flat" alt="295 tests">
  <a href="https://claude.com/claude-code"><img src="https://img.shields.io/badge/Built_with-Claude_Code-000?style=flat&logo=anthropic&logoColor=white" alt="Built with Claude Code"></a>
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT">
  <a href="DISCLAIMER.md"><img src="https://img.shields.io/badge/%E2%9A%A0%EF%B8%8F-not_investment_advice-critical" alt="Not investment advice"></a>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="meigu-ops demo" width="900">
</p>

<p align="center">
  <sub>Every number and every tag in the demo is a fabricated placeholder (<code>examples/</code> fixtures).<br>
  The last scene replays a real incident shape: the intent was a <em>partial</em> trim, but the position
  had shrunk to one standard size — the gate computed <strong>90.9%</strong> and denied it.</sub>
</p>

> **Note:** the operational content (`modes/`, `templates/`, reports) is written in Chinese,
> since that's the working language of the account it was built for. Code, config schemas,
> and this README are in English so the architecture is legible to everyone.

---

## ⚠️ What this project deliberately does NOT provide

**It does not provide a trading strategy.**

You will not find answers here to "when to buy", "how much cash to hold", or "what
single-name cap to use". Not because they're withheld, but because **those answers are
meaningless to you** — account size, risk tolerance, universe, and screen time all differ.
Copying someone else's rules is more dangerous than having none: you'd execute a rule
without understanding why it holds, and you'd neither notice nor be able to fix it when
it stops holding.

What the repository provides is **the machinery that lets your own discipline be tested
by your own ledger, and corrected because of it**:

```
you write a rule ──► cited during daily decisions ──► trades hit the ledger ──► audited on review
       ▲                                                                              │
       └──────────────── promote / demote / retire (requires your approval) ◄─────────┘
```

## What it does

```
9:12   /meigu-ops premarket   Plan: candidates, triggers, defense, "what not to do today"
10:33  /meigu-ops check       Intraday: three-question filter → buy / sell / hold
13:03  /meigu-ops check
15:37  /meigu-ops check
16:06  /meigu-ops journal     Close: journal + ledger + tiered compression + evidence
       /meigu-ops daily       Closing report (15 sections, skeleton driven by your config)
       /meigu-ops review      Periodic review → audit every one of your rules against data
       /meigu-ops stats       FIFO realized P&L / performance by tag / rule audit
```

It does not forecast markets. It does something else: **it guarantees today's judgment
won't be worse than last time's.**

## Core features

| Feature | Description |
|---|---|
| **Strategy layer fully private** | Tag vocabulary, rules, prose strategy, account params are all gitignored. The repo ships only `*.example.*` templates — question lists with zero answers |
| **Rules audited by data** | Write discipline as falsifiable entries; `make stats` decides whether your ledger supports them. **A rule never supported by data is baggage, not an asset** |
| **The audit refuses sloppy conclusions** | Direction and confidence are separate: < 10 decision events → no conclusion; 10–19 → "weakly supports/refutes" only; ≥ 20 → may *suggest* a status change — and every change needs your approval |
| **Position size scales with evidence** | Evidence strength governs **size**, not whether you may trade — otherwise day one deadlocks (no data → no trading → never accumulate data). Unvalidated hypotheses run at 40% size, weak support 70%, supported full. **Fully automatic; no manual downsizing** |
| **Sample unit is the decision event** | One sell matched against three historical buy lots is still **one** exit decision — not three samples |
| **Deterministic order gates** | `preflight.py` turns intent TTL, broker-quote-timestamp fuse, reduce-percentage, concentration, daily caps, and `ref_id` dedup into a **program**, returning `ALLOW`/`DRY_RUN`/`DENY` |
| **Execution off by default** | `execution.enabled = false` is the repository default — cloning inherits nobody's authorization. Authorization is a local fact |
| **Platform-mechanics knowledge base** | `modes/_mechanics.md` collects how Robinhood MCP + Claude Code turn a correct decision into a failed execution: settlement, timestamp fuse, sleep, timezone drift, permission prompts freezing the session |
| **Read-only dashboard TUI** | Portfolio / ledger / rule audit. curses, zero deps, and the whole UI is pure functions so it's fully test-covered |
| **Report skeleton is config-driven** | Indices, sectors, themes, groups, risk dimensions all come from your config. The template names no industry and no ticker |
| **Zero dependencies** | Python 3.11+ stdlib. `tomllib` for config, `zoneinfo` for ET |

## Quick start

```bash
git clone https://github.com/jjMurphy1012/meigu-ops.git
cd meigu-ops

cp config/profile.example.toml      config/profile.toml
cp config/watchlist.example.toml    config/watchlist.toml
cp config/reason-tags.example.toml  config/reason-tags.toml
cp modes/_strategy.example.md       modes/_strategy.md      # answer the questions inside
cp config/rules.example.toml        config/rules.toml

make doctor
make rules-check
make test
make report
```

**Step four is the point of this project, and the slowest step.** `_strategy.example.md`
is all questions and no answers — those answers must be yours. Start with **2–3 rules you
believe most**. Ten untested rules are worth less than two the data has supported.

Full setup: [docs/SETUP.md](docs/SETUP.md).

## Data boundary

```
┌──── ✅ System layer (open source) ────┐  ┌── ❌ User layer (never committed) ──┐
  AGENTS.md   modes/_mechanics.md          config/profile.toml     account + auth
  modes/{premarket,check,trade,...}.md     config/reason-tags.toml your tag vocabulary
  templates/  scripts/  tests/  docs/      config/rules.toml       your rules
  config/*.example.*                       modes/_strategy.md      your strategy
  modes/_strategy.example.md               data/  reports/         ledger/journal/reports
```

The test is one question: **is this "how to do it" or "what was done"?**
"How you categorize your exits, and which rules you believe" is the latter.
Full contract: [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md).

## Scope

- **Broker**: Robinhood MCP only; no multi-broker abstraction.
- **Market**: US equities. Calendar computed from NYSE/Nasdaq rules (excludes ad-hoc closures).
- **Style-agnostic**: report skeleton, tag vocabulary, and rules all come from your config.
- **Size**: designed for a self-managed small account (fractional shares, market-order
  slippage, and cash-account T+1 settlement are all accounted for).

## ⚠️ Disclaimer

**This project does not provide investment advice.** It is a personal analysis workflow and
documentation toolkit. It does not forecast markets and does not recommend buying, selling,
or holding any security. Every ticker, level, and tag in the repository is a structural
example or placeholder; all figures in `examples/` and `demo/` are fabricated.

**This project can submit real orders to a brokerage.** Before enabling that, verify API
permissions, order types, size limits, settlement rules, and fallback behavior yourself.
**Any resulting gains or losses are entirely your own.**

See [DISCLAIMER.md](DISCLAIMER.md).

## License

[MIT](LICENSE)
