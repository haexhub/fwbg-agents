# fwbg-agents

Autonomous agent service for fwbg strategy research, backtesting, paper trading, and live trading.

## Architecture reference

The complete design lives in the fwbg repo at `docs/plans/2026-06-23-fwbg-agents-design.md`. Read it before making non-trivial changes.

## Working with this codebase

- Python 3.12+, uv-managed. Use `uv add <pkg>` to add dependencies, `uv sync` to install.
- FastAPI + asyncio. Agents are async I/O-bound tasks, not threads.
- State persistence: SQLite (`data/state.db`) for metadata, filesystem (`data/strategies/`, `data/plugins/`) for artifacts.
- Lifecycle state machines for strategies and plugins are the central abstraction. Never bypass `orchestrator/lifecycle.py` for state transitions.
- LLM calls go through the Anthropic SDK pointed at `haex-claude-proxy` via `ANTHROPIC_BASE_URL`. Never hard-code provider URLs.

## Critical safety rules

- **No `DELETE` endpoints for strategies or plugins.** Only soft-abandon via state transitions. Reasons: traceability and anti-redundancy (researcher must not re-propose abandoned ideas).
- **Stop-loss is mandatory for every order, paper or live.** Pre-trade validators reject orders without SL. SL is sent atomically with entry.
- **Live trading requires human approval gate.** Backtest → paper can be automated; paper → live always needs manual confirmation in dashboard.
- **Generated plugins live in `data/plugins/` only.** They are never auto-committed to the fwbg core repo. The PromoteAgent opens a PR for human review.
