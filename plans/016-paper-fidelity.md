# Plan 016: Make paper trading measure fidelity, not just P&L

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 75123b0..HEAD -- src/fwbg_agents/tools/fwbg_paper_reader.py src/fwbg_agents/orchestrator/criteria_paper.py data/criteria/paper/`
> (fwbg-agents) and, for Part B,
> `git diff --stat 8ab08f7..origin/main -- src/fwbg/bot.py` (fwbg, after
> `git fetch origin`). On mismatch with "Current state", STOP.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW–MED (additive metrics; only the criteria-YAML wiring changes gate behavior)
- **Depends on**: none (independent of 014/015)
- **Category**: bug (metric validity) / direction
- **Planned at**: fwbg-agents commit `75123b0`, fwbg commit `8ab08f7` (origin/main), 2026-07-16

## Why this matters

Paper trading's job in a backtest→paper→live funnel is to validate that the
backtest's cost and fill assumptions survive contact with a real broker feed.
Today it can't do that, in two independent ways:

- **Part A — the paper Sharpe is a different quantity than the one the
  strategy was selected on.** `sharpe_paper` annualizes per-**trade** P&L
  percentages with `sqrt(252)`, treating each trade as one daily return. The
  backtest side (promote gate, DSR) works strictly per-trade
  (mean/std of the trade-P&L series, no annualization — see
  `orchestrator/trials.py` "Unit discipline" docstring). The paper criteria
  thresholds therefore gate on a number that is not comparable to anything
  upstream, and its scale varies with trade frequency.
- **Part B — no fill/spread fidelity.** The backtest assumes a single static
  spread (90th percentile of ask−bid at download time) and slippage derived
  from it. The paper bot records only fill price and quantity — not what the
  signal wanted, not the spread assumption. A strategy whose backtest edge is
  smaller than the real spread/slippage gap can pass paper on raw P&L and can
  therefore still reach live. After this plan, paper telemetry records the
  intended price and the assumed spread per trade, and the paper summary
  reports realized-vs-assumed divergence that criteria can gate on.

## Current state

### fwbg-agents

- `src/fwbg_agents/tools/fwbg_paper_reader.py` — reads
  `<FWBG_DATA_DIR>/account-trades/<slug>/{trades.jsonl,status.json,positions.json}`.
  Module docstring says formulas are intentionally inlined ("we do NOT import
  from `fwbg`") — keep that decoupling.
  - Lines 111–119, the Part-A defect:

    ```python
    def _compute_sharpe(pnls: list[float]) -> float:
        """Annualised Sharpe assuming ~252 trading days. 0.0 if undefined."""
        if len(pnls) < 2:
            return 0.0
        mean = statistics.mean(pnls)
        std = statistics.pstdev(pnls)
        if std == 0:
            return 0.0
        return (mean / std) * math.sqrt(252)
    ```

  - `PaperTradeSummary` (lines ~33–46) is the pydantic model consumed by the
    criteria evaluator; fields include `sharpe_paper`, `max_dd_paper`,
    `trades_total`, `win_rate`, …
  - `read_paper_summary` (lines ~144 ff.) collects `pnl_values` from each
    trade's `pnl_pct` field and calls `_compute_sharpe(pnl_values)`.
- `src/fwbg_agents/orchestrator/criteria_paper.py` — evaluates
  `summary.model_dump()` against `data/criteria/paper/<asset_class>.yaml`
  (`required_all` + `hard_blockers`; missing metric = failure, i.e. adding new
  YAML keys before the summary provides them would break the gate — order
  matters).
- `data/criteria/paper/*.yaml` — the thresholds (inspect what exists:
  `ls data/criteria/paper/`).
- The paper analyst prompt (find with `ls prompts/` — the paper-analyst
  template) receives the summary; check `agents/paper_analyst.py` for how the
  summary is rendered.

### fwbg (local checkout is stale — branch from `origin/main` after `git fetch origin`)

- `src/fwbg/bot.py:817-850` — `_record_trade` writes one JSONL line per
  filled entry:

  ```python
  entry = {
      "trade_id": str(uuid.uuid4()),
      "strategy_slug": self.strategy_slug,
      "symbol": symbol,
      "side": direction.value.lower(),
      "entry_time": datetime.now(timezone.utc).isoformat(),
      "exit_time": None,
      "entry_price": float(fill_price) if fill_price else None,
      "exit_price": None,
      "pnl_pct": None,
      "quantity": float(size),
      "fees": 0.0,
  }
  ```

  No signal/intended price, no spread assumption. Best-effort (never raises).
- The assumed spread per symbol is persisted by
  `fwbg.data.assets.save_asset_spread` at download time
  (`src/fwbg/data/dukascopy.py:349-357`, 90th-percentile ask−bid close gap);
  there is a corresponding load path in `fwbg/data/assets.py` (verify the
  getter's name before using it).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| agents tests | `uv run pytest -q` | all pass |
| agents lint/types | `uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src` | exit 0 |
| fwbg tests | `uv run pytest -q` | all pass |
| fwbg lint | `uv run ruff check src/ packages/` | exit 0 |

## Scope

**In scope**:
- fwbg-agents: `tools/fwbg_paper_reader.py`, the paper-analyst rendering of
  the summary (display only), `data/criteria/paper/*.yaml` (add the new keys
  **commented out** — see Step 4), tests.
- fwbg: `src/fwbg/bot.py` (`_record_trade` and its call site(s) only), tests
  for the telemetry entry.

**Out of scope**:
- Do NOT change existing YAML thresholds or remove `sharpe_paper` — threshold
  recalibration is the maintainer's call.
- `criteria_paper.py` evaluator logic (it already handles any summary field).
- The bot's order execution, adapters, equity sampling.
- Backtest-side spread modeling (session-aware spreads etc. — separate,
  larger topic).

## Git workflow

- fwbg-agents: branch `advisor/016-paper-fidelity` off `develop`.
- fwbg: branch `advisor/016-paper-telemetry` off `origin/main` (NOT the stale
  local checkout). Conventional commits; no Claude/Anthropic references.
- Do not push / open PRs unless the operator says so.

## Steps

### Step 1 (fwbg): record intent + assumption per trade

In `bot.py::_record_trade`, extend the entry with:

- `"signal_price"`: the price the signal intended to trade at. Inspect the
  call site of `_record_trade` (grep `_record_trade(` in `bot.py`): if the
  signal/decision price is available there (the signal event or the order's
  reference price), thread it through as a new parameter with default `None`.
- `"assumed_spread"`: the stored asset spread for `symbol` at entry time
  (load via the `fwbg.data.assets` getter that pairs with
  `save_asset_spread`; wrap in try/except → `None`, keeping the method's
  best-effort contract).

Both fields default to `None` so old readers are unaffected (JSONL is
append-only and schemaless).

**Verify**: fwbg test asserting the JSONL entry contains both keys when
provided, and that a `None` spread doesn't raise. Model after whatever
existing test covers `_record_trade`/telemetry (find:
`grep -rln "trades.jsonl" tests/`); if none exists, add
`tests/test_bot_telemetry.py` with a minimal bot fixture.

### Step 2 (fwbg-agents): per-trade Sharpe alongside the annualized one

In `fwbg_paper_reader.py`:

- Add `_compute_sharpe_per_trade(pnls)` — `mean/pstdev`, **no** `sqrt(252)`;
  `0.0` if undefined (mirror `_compute_sharpe`'s guards).
- Add field `sharpe_paper_per_trade: float` to `PaperTradeSummary` and fill it
  in `read_paper_summary`. Keep `sharpe_paper` untouched (backward compat;
  existing YAMLs reference it).
- Fix `_compute_sharpe`'s docstring to state explicitly: "annualised by
  sqrt(252) treating each trade as one daily return — NOT comparable to the
  backtest's per-trade Sharpe; prefer `sharpe_paper_per_trade` for
  backtest-vs-paper comparisons."

**Verify**: unit test with a fixed P&L list asserts
`sharpe_paper == sharpe_paper_per_trade * sqrt(252)` and both are 0.0 for
`len < 2`.

### Step 3 (fwbg-agents): fidelity metrics in the summary

In `read_paper_summary`, from trades that carry the new fields:

- `entry_slippage` per trade = `abs(entry_price - signal_price)` where both
  are finite numbers; expected cost per side = `assumed_spread / 2`.
- New `PaperTradeSummary` fields (all `float | None`, `None` when no trade
  has the data yet — old JSONL files must keep working):
  - `avg_entry_slippage: float | None`
  - `avg_assumed_half_spread: float | None`
  - `fill_fidelity_ratio: float | None` — `avg_entry_slippage / avg_assumed_half_spread`
    (`None` if the denominator is 0/None). Ratio ≤ 1.0 means fills are within
    the backtest's cost assumption; > 1.0 means the backtest was optimistic.
  - `fidelity_sample_size: int` — trades contributing to the ratio.
- Guard everything with `math.isfinite`; skip non-conforming trades.

**Verify**: unit tests — trades with/without the new fields mixed; all-legacy
file → all fidelity fields `None`, `fidelity_sample_size == 0`.

### Step 4 (fwbg-agents): surface, don't yet gate

- Paper-analyst prompt rendering: wherever the summary is rendered for the
  paper analyst (check `agents/paper_analyst.py`), include the three fidelity
  numbers and `sharpe_paper_per_trade` with one explanatory line ("fidelity
  ratio > 1.0 = real fills cost more than the backtest assumed").
- In each `data/criteria/paper/*.yaml`, add a commented-out example rule the
  maintainer can enable after observing real values:

  ```yaml
  # Enable after calibrating against observed paper fills (plan 016):
  # required_all:
  #   - fill_fidelity_ratio: "<= 1.5"
  #   - sharpe_paper_per_trade: ">= 0.05"
  ```

  Do NOT add active rules — `evaluate_paper_criteria` counts a missing metric
  as a failure, and legacy strategies without new-format trades would
  hard-fail the gate.

**Verify**: `uv run pytest -q` all green; `grep -rn "fill_fidelity_ratio" data/criteria/paper/` shows only commented lines.

### Step 5: full gates

**Verify** (fwbg-agents): `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src` → exit 0.
**Verify** (fwbg): `uv run pytest -q && uv run ruff check src/ packages/` → exit 0.

## Test plan

- fwbg: `_record_trade` writes `signal_price`/`assumed_spread`; missing spread
  → `None`, no exception.
- fwbg-agents (`tests/tools/test_fwbg_paper_reader.py` — extend the existing
  module; it already covers `read_paper_summary` with tmp JSONL fixtures):
  - per-trade vs annualized Sharpe relation (Step 2).
  - fidelity aggregation: 2 conforming + 1 legacy trade → correct averages
    and `fidelity_sample_size == 2`.
  - zero half-spread → `fill_fidelity_ratio is None`.
  - all-legacy input → summary identical to before except new `None` fields
    (regression guard for the paper gate).

## Done criteria

- [ ] Both repos: tests, lint (and mypy on agents) exit 0
- [ ] `PaperTradeSummary` has `sharpe_paper_per_trade`, `avg_entry_slippage`, `avg_assumed_half_spread`, `fill_fidelity_ratio`, `fidelity_sample_size`; all `None`/0-safe on legacy data
- [ ] fwbg `trades.jsonl` entries include `signal_price` and `assumed_spread`
- [ ] No active criteria rule added (commented examples only)
- [ ] No files outside the in-scope list modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- The signal/intended price is genuinely not reachable at `_record_trade`'s
  call site without touching order-execution code — report what IS available
  (e.g. only the order's requested level) and wait for direction.
- `fwbg.data.assets` has no spread **getter** (only the saver) — report
  instead of inventing a file-format reader.
- Any existing paper-gate test fails after Step 3 — the summary shape is load-
  bearing; report the failing expectation rather than adapting it silently.
- The paper-analyst rendering turns out to inline the summary via
  `model_dump()` wholesale (then new fields flow automatically) — that's fine,
  but if it instead lists fields explicitly in a prompt template you cannot
  find, report.

## Maintenance notes

- The fidelity ratio only becomes a **gate** when the maintainer enables the
  YAML rules — revisit after a few weeks of paper data
  (`fidelity_sample_size` tells you when there's enough).
- If fwbg later adds exit-fill recording (`exit_price` is currently written as
  `None` at entry and updated elsewhere — verify), extend slippage to exits.
- Backtest-side improvement deferred: session-aware/dynamic spreads in
  `dukascopy.py` would narrow the assumed-vs-real gap at the source.
- Interaction with plan 019: its quality report includes the spread the
  backtest assumes; the fidelity ratio here is the live counterpart.
