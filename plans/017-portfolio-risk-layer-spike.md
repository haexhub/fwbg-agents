# Plan 017: Design spike — portfolio/correlation risk layer before live capital

> **Executor instructions**: This is a **design spike, not a build**. The
> deliverable is a design document plus a small data-feasibility script — no
> production code changes. Follow the steps; on any STOP condition, stop and
> report. When done, update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 75123b0..HEAD -- src/fwbg_agents/api/strategies.py src/fwbg_agents/orchestrator/trade_diagnostics.py src/fwbg_agents/orchestrator/live_catalog.py`
> Drift here is informational, not blocking — this spike reads code, it
> doesn't change it.

## Status

- **Priority**: P2 (next risk layer — before real live capital is scaled)
- **Effort**: M (spike; the eventual build is L and out of scope)
- **Risk**: LOW (no production code changes)
- **Depends on**: none (reads outputs of 014/016 if they've landed; works without)
- **Category**: direction
- **Planned at**: commit `75123b0`, 2026-07-16

## Why this matters

Every gate in the pipeline — backtest criteria, promote gate, paper criteria,
human approval — evaluates one strategy in isolation. The researcher fan-out
diversifies *research* (exploration-balance pressure over
`strategy_family×asset_class×timeframe`), but nothing diversifies the *live
book*: N strategies sharing one edge mechanism and highly correlated returns
can each individually clear every gate and go live, concentrating capital in
one bet. There is also no portfolio-level position-sizing policy. This is
explicitly **missing risk architecture**, not a bug in existing code — the
spike's job is to design the layer and prove the required inputs exist, so
the maintainer can decide on a concrete proposal instead of an idea.

## Current state (evidence the spike starts from)

- Per-strategy live gate: `src/fwbg_agents/api/strategies.py:440-527`
  (`post_strategy_promote_live`) — human approval + analyst flag + state
  check, all scoped to the single strategy. No cross-strategy input of any
  kind.
- Per-trade return series already load from disk:
  `src/fwbg_agents/orchestrator/trade_diagnostics.py::_load_symbol_trades`
  (reads `grid_details/<sym>/fold_results.json` → `test_trades_detail`
  with `entry_time`/`exit_time`/`pnl_raw` per trade) — the natural
  correlation input for backtest/holdout data.
- Paper-side series: `src/fwbg_agents/tools/fwbg_paper_reader.py`
  (`trades.jsonl` per strategy, `pnl_pct` + `entry_time`).
- Live inventory: `src/fwbg_agents/orchestrator/live_catalog.py` knows which
  strategies exist fwbg-side; the `Strategy` table knows which are
  `LIVE_TRADING`/`PAPER_TRADING` (`persistence/models.py`, `StrategyState`).
- fwbg exposes a `risk_management` plugin phase (see
  `fwbg/src/fwbg/plugins/fwbg-core/manifest.json`) — a potential enforcement
  point for per-strategy sizing, but nothing consumes cross-strategy
  correlation today.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| agents tests (unchanged) | `uv run pytest -q` | all pass (nothing modified) |
| run the feasibility script | `uv run python scripts/spike_portfolio_correlation.py --help` | usage text |

## Scope

**In scope** (files you may create):
- `docs/plans/2026-07-portfolio-risk-layer.md` (the design doc)
- `scripts/spike_portfolio_correlation.py` (read-only feasibility script)

**Out of scope** (do NOT touch):
- Any production module, any test, any migration, any criteria YAML, both
  repos' existing code. The spike must leave `git status` clean except the
  two new files.

## Git workflow

- Branch `advisor/017-portfolio-spike` off `develop`. Conventional commits;
  no Claude/Anthropic references. No push/PR unless instructed.

## Steps

### Step 1: feasibility script — can we compute the correlation input today?

Write `scripts/spike_portfolio_correlation.py` (read-only; stdlib + pandas,
both already dependencies):

- For each strategy in states `PAPER_TRADING`/`LIVE_TRADING`/`BACKTESTED`
  (CLI flag `--states`), load its most recent per-trade series: prefer paper
  `trades.jsonl`, fall back to the latest iteration's holdout/backtest trades
  via the `fold_results.json` path used by `trade_diagnostics`.
- Resample each series to daily P&L (sum of `pnl_raw`/`pnl_pct` per calendar
  day — document the unit mismatch between the two sources in the output).
- Print: per-pair Pearson correlation matrix of overlapping days, the number
  of overlapping days per pair, and a warning for pairs with < 20 overlapping
  days ("correlation not estimable").

**Verify**: script runs against the local `data/` dir without exceptions and
prints a matrix (or a clear "no eligible strategies" message on an empty DB).
`git status` shows only the new files.

### Step 2: the design document

Write `docs/plans/2026-07-portfolio-risk-layer.md` covering, concretely:

1. **Correlation input decision** — paper trades vs holdout trades vs both;
   minimum overlap; unit normalization (pnl_raw vs pnl_pct). Ground it in
   what Step 1 actually found (data density, overlap counts from real data).
2. **The gate** — proposal: a `portfolio_check` that runs inside
   `post_strategy_promote_live` before the human gate: reject (or warn) when
   max pairwise correlation of the candidate against any live strategy
   exceeds a ceiling (propose a default, e.g. 0.6, and say why), or when the
   live book already holds ≥ K strategies of the same
   `strategy_family×asset_class`. State explicitly that the check must be a
   **deterministic code gate** (like the promote gate), not an LLM judgment.
3. **Sizing policy options** — at least: equal risk-budget per strategy
   (equity/N scaled by per-strategy realized vol) vs correlation-penalized
   weights; who enforces it (fwbg `risk_management` plugin phase vs
   agents-side account config) and what each would touch.
4. **Failure modes** — correlation estimated on thin/overlapping-window data,
   regime-dependent correlation, the candidate having only backtest data.
5. **Open questions for the maintainer** — numbered, each with a recommended
   answer.
6. **Build estimate** — S/M/L per component, and what plans it would split
   into.

Follow the structure of the existing design docs in `docs/plans/` (e.g.
`2026-07-03-preset-crystallization.md`) — problem, proposal, alternatives,
open questions.

**Verify**: doc exists; every section 1–6 present
(`grep -c '^## ' docs/plans/2026-07-portfolio-risk-layer.md` ≥ 6).

## Test plan

None (no production code). The feasibility script is itself the evidence; it
must run cleanly on the real local data dir.

## Done criteria

- [ ] `scripts/spike_portfolio_correlation.py` runs without error locally
- [ ] Design doc exists with all six sections and grounded numbers from Step 1
- [ ] `git status` clean except the two new files
- [ ] `plans/README.md` status row updated

## STOP conditions

- Fewer than 2 strategies have any per-trade data at all → the correlation
  layer has no input yet; write that finding into the doc's section 1 and
  mark the gate proposal as "blocked on data density", then finish the doc
  anyway (this is a downgrade, not an abort).
- `fold_results.json` files lack `entry_time`/`exit_time` on this
  installation's data (older runs) → note which runs, use paper data only.

## Maintenance notes

- Revisit after plan 016 lands: fidelity-filtered paper series are the better
  correlation input.
- The eventual build should reuse `_load_symbol_trades` rather than a new
  parser — note this in the design doc.
