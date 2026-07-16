# Plan 015: Persist trial statistics durably and make the Deflated-Sharpe gate fail closed

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 75123b0..HEAD -- src/fwbg_agents/orchestrator/trials.py src/fwbg_agents/orchestrator/promote_gate.py src/fwbg_agents/agents/runner.py src/fwbg_agents/api/trials.py src/fwbg_agents/persistence/models.py`
> If plan 014 already landed, its edits to `promote_gate.py`/`runner.py` are
> expected drift — re-read those two files before starting. Any other
> mismatch with the "Current state" excerpts is a STOP condition.

## Status

- **Priority**: P0/P1
- **Effort**: M
- **Risk**: MED (the gate becomes stricter; promote rate drops by design)
- **Depends on**: 014 (both edit `promote_gate.py`; 014 first)
- **Category**: bug (statistical validity)
- **Planned at**: commit `75123b0`, 2026-07-16

## Why this matters

The Deflated Sharpe Ratio (DSR) is the promote gate's only population-level
multiple-testing guard: a candidate's holdout Sharpe must beat the expected
max Sharpe of N zero-skill trials. Three defects make it systematically
lenient exactly when overfitting risk is highest:

1. **Retention erodes the inputs.** Trial counts are recomputed from
   `data/strategies/*/iteration_*/fwbg_results.json` sidecars and the
   cross-trial Sharpe variance from fwbg run dirs that still exist on disk —
   the run janitor prunes those, shrinking `n_trials` and the variance sample
   over time. Both shrinkages **lower** the bar.
2. **The docstring's claim is backwards.** `trials.py` says undercounting
   makes the gate "honest-or-stricter". It's the opposite: fewer trials →
   smaller `E[max SR]` → the candidate deflates against a lower benchmark →
   **softer** gate.
3. **NaN fails open.** `json.loads` accepts the `NaN` token,
   `isinstance(nan, float)` is `True`, `series_moments` has no finite-guard
   (`NaN == 0` is `False`), and `NaN < dsr_min` is `False` — so a single
   malformed trade P&L value anywhere in the holdout run or the variance
   sample silently passes the DSR check. Additionally, with fewer than 2
   surviving historical Sharpes the check passes trivially (pass-open).

Fix: snapshot each completed run's trial count and per-trade Sharpe into a
durable DB table at run completion (plus a one-shot backfill), compute the DSR
from that table, and treat non-finite values as gate failure. Side effect:
`count_trials` stops doing an unbounded synchronous filesystem scan on the
event loop (today it blocks the whole FastAPI/SSE/auto_runner loop, cost
growing with the run corpus).

## Current state

- `src/fwbg_agents/orchestrator/trials.py` — everything lives here.
  - Docstring (lines ~9–24) contains the claims to fix, including:
    "assets that don't expose them (`0`/missing) count conservatively as one
    trial each — that undercounts true search breadth, so the resulting DSR is
    an *upper* bound and the gate stays honest-or-stricter as artifacts
    improve." and "whose fwbg run dirs still exist (retention may have pruned
    older ones …)".
  - `pnl_series(run_dir)` (lines 69–80): reads
    `grid_details/<sym>/fold_results.json` per symbol via
    `_load_symbol_trades` (imported from `orchestrator/trade_diagnostics.py`,
    which accepts any `isinstance(t.get("pnl_raw"), (int, float))` — NaN
    included) and returns `[float(t["pnl_raw"]) …]`.
  - `count_trials(session)` (lines 93–129): `async` but fully synchronous
    body; globs all `data/strategies/*/iteration_*/fwbg_results.json`, parses
    each, and per run re-reads all fold files via `pnl_series` to compute
    `per_trade_sharpe`. Returns `TrialCounts(global_runs, global_trials,
    by_family, trade_sharpes)`.
  - `series_moments(pnls)` (lines 172–184): no `isfinite` anywhere; a NaN in
    `pnls` propagates to `sr/skew/kurtosis`.
  - `_trials_in_run(run_data)` (lines 57–66): sums per-asset
    `total_combinations`, counting 1 where missing.
- `src/fwbg_agents/orchestrator/promote_gate.py:101-141` — `_run_dsr_check`:

  ```python
  pnls = pnl_series(settings.fwbg_test_results_dir / holdout_job_id)
  moments = series_moments(pnls)
  if moments is None:
      return True, None, None, []
  sr, skew, kurtosis = moments
  counts = await count_trials(session)
  if len(counts.trade_sharpes) < 2:
      return True, None, counts.global_trials, []
  sr_variance = statistics.variance(counts.trade_sharpes)
  dsr = deflated_sharpe_ratio(...)
  if dsr < settings.dsr_min:
      return (False, dsr, counts.global_trials, [...])
  return True, dsr, counts.global_trials, []
  ```

- `src/fwbg_agents/agents/runner.py:307` — the completion point where an
  iteration backtest writes its sidecar:
  `results_path = iteration_dir / "fwbg_results.json"`. The run's
  `run_data` dict and run dir are in scope there.
- `src/fwbg_agents/api/trials.py` — `GET /trials/summary` calls
  `count_trials` and exposes `n_trials` / `sr_variance_across_trials` /
  `sr_variance_sample_size`. Its response model must keep the same fields.
- Persistence conventions:
  - Models in `src/fwbg_agents/persistence/models.py` (see `CalibrationRun`
    around line 138 for a small standalone table exemplar; `UtcDateTime()`
    for timestamps).
  - Migrations under `src/fwbg_agents/persistence/migrations/versions/`,
    latest is `0010_settings_table.py`; header/revision pattern:

    ```python
    revision: str = "0010"
    down_revision: str | Sequence[str] | None = "0009"
    ```

  - Dev scripts live in `scripts/` (exemplar: `scripts/backfill_plugin_specs.py`).
- The run janitor that prunes run dirs is
  `src/fwbg_agents/orchestrator/run_janitor.py` (do not modify it — the point
  of this plan is that pruning becomes harmless).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest -q` | all pass |
| Lint | `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts` | exit 0 |
| Types | `uv run mypy src` | exit 0 |
| Migration | `uv run alembic upgrade head` | applies `0011` cleanly |

## Scope

**In scope**:
- `src/fwbg_agents/persistence/models.py` (new `TrialStat` model)
- `src/fwbg_agents/persistence/migrations/versions/0011_trial_stat.py` (create)
- `src/fwbg_agents/orchestrator/trials.py`
- `src/fwbg_agents/orchestrator/promote_gate.py` (`_run_dsr_check` only)
- `src/fwbg_agents/agents/runner.py` (insert-at-completion hook only)
- `src/fwbg_agents/api/trials.py` (switch to DB-backed census; same response shape)
- `scripts/backfill_trial_stats.py` (create)
- tests

**Out of scope**:
- `orchestrator/trade_diagnostics.py` — its permissive `pnl_raw` acceptance is
  used by the Analyst diagnostics too; filter NaN on the **trials side**
  instead (don't change diagnostics behavior).
- `run_janitor.py`, retention settings.
- The holdout window logic (plan 014), criteria YAMLs, `dsr_min`'s value.
- fwbg repo entirely.

## Git workflow

- Branch `advisor/015-durable-trial-stats` off `develop` (after 014 merged, or
  rebased onto 014's branch if instructed).
- Conventional commits; no Claude/Anthropic references in messages.
- Do not push or open PRs unless the operator says so.

## Steps

### Step 1: `TrialStat` model + migration 0011

New model in `models.py` (place near `CalibrationRun`):

```python
class TrialStat(Base):
    """Durable per-backtest search-breadth snapshot (survives run-dir pruning)."""

    __tablename__ = "trial_stat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    strategy_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("strategy.id"), nullable=True, index=True)
    strategy_family: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    n_trials: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
```

Migration `0011_trial_stat.py` (revision `0011`, down_revision `0010`),
mirroring `0010_settings_table.py`'s structure.

**Verify**: `uv run alembic upgrade head` → exit 0; `uv run alembic downgrade -1 && uv run alembic upgrade head` → clean round-trip.

### Step 2: record a `TrialStat` at every run completion

Add to `trials.py`:

```python
async def record_trial_stat(session, *, run_id, strategy, run_data, run_dir) -> None:
    """Insert-or-ignore one TrialStat for a completed backtest. Best-effort:
    log and continue on failure — recording stats must never fail a run."""
```

- `n_trials = _trials_in_run(run_data)`; `pnls = [x for x in pnl_series(run_dir) if math.isfinite(x)]`;
  `trade_sharpe = per_trade_sharpe(pnls)`; `n_trades = len(pnls)`.
- Insert-or-ignore on `run_id` (query-first is fine; unique constraint is the
  backstop).

Call it from `runner.py` right after the `fwbg_results.json` sidecar is
written (line ~307 context) — the runner has `session`, the strategy, the
parsed `run_data`, and can build
`run_dir = settings.fwbg_test_results_dir / run_data["run_id"]` the same way
`count_trials` does today. Also call it for the promote gate's holdout and
cost-stress runs: in `promote_gate.py`, after each successful
`runner.execute_backtest(...)` returns `(job_id, run_data)`, record a stat
with that `job_id` (gate runs are search trials too — plan 014's review noted
they were previously uncounted).

**Verify**: new unit test — run the runner's completion path against a tmp dir
fixture (follow the existing runner test fixtures; find them with
`grep -rl execute_backtest tests/ | head`) and assert a `trial_stat` row with
the expected `n_trials`/`n_trades` exists; inserting the same `run_id` twice
leaves one row.

### Step 3: DB-backed census

Rewrite `count_trials(session)` to aggregate from `trial_stat` (SQL only — no
filesystem access):

- `global_runs` = row count; `global_trials` = `sum(n_trials)`;
  `by_family` = group-by `strategy_family`;
  `trade_sharpes` = all non-NULL, finite `trade_sharpe` values.
- Keep the exact `TrialCounts` shape so `api/trials.py` and
  `promote_gate.py` compile unchanged.
- Rewrite the module docstring: remove the "honest-or-stricter" claim and
  state the true direction — "undercounting `n_trials` lowers E[max SR] and
  therefore **weakens** the gate; the durable census exists to prevent
  silent undercounting from retention pruning. Assets that don't expose
  `total_combinations` still count as 1 — a known, explicit undercount."

**Verify**: `uv run pytest -q` → existing `api/trials.py` tests pass
unchanged (find them via `grep -rl "trials/summary" tests/`); temporary
seed-rows fixture replaces any filesystem fixtures.

### Step 4: backfill script

`scripts/backfill_trial_stats.py` — one-shot, idempotent: reuse the OLD scan
logic (glob `data/strategies/*/iteration_*/fwbg_results.json`, compute
`_trials_in_run` + finite-filtered `per_trade_sharpe`) and insert missing
`trial_stat` rows. Model the script's structure (arg parsing, session setup,
logging, `if __name__ == "__main__"`) on `scripts/backfill_plugin_specs.py`.
Print a summary line: `backfilled N rows, skipped M existing`.

**Verify**: `uv run python scripts/backfill_trial_stats.py --help` → exits 0
and prints usage. (Running against real data happens at deploy time, not in
this plan.)

### Step 5: fail-closed DSR

In `promote_gate.py::_run_dsr_check`:

- Filter the holdout series: `pnls = [x for x in pnl_series(...) if math.isfinite(x)]`.
- After computing `dsr`: `if math.isnan(dsr): return False, None, counts.global_trials, ["dsr is NaN — non-finite inputs; failing closed"]`.
- Guard `sr_variance`: if it is not finite, same fail-closed return.
- Keep the `<2 trade_sharpes` pass-open branch (cold-start; the durable census
  makes this window short-lived) but make it visible: return
  `(True, None, counts.global_trials, ["dsr skipped: <2 historical trial sharpes (pass-open)"])`
  — note the `failures` list is also used for the passing GateRun's display,
  so rename nothing; the string lands in the sidecar where the dashboard can
  show it. Update `_run_dsr_check`'s docstring accordingly.

In `trials.py::series_moments`, add a defensive
`if not all(math.isfinite(x) for x in pnls): return None` at the top (cheap,
and protects the other caller).

**Verify**: new tests (see Test plan) — a NaN P&L value in the holdout fold
fixture makes the gate FAIL; the sidecar contains the fail-closed message.

### Step 6: full gates

**Verify**: `uv run pytest -q && uv run ruff check src tests scripts && uv run ruff format --check src tests scripts && uv run mypy src` → all exit 0.

## Test plan

New tests in the existing promote-gate/trials test modules (find with
`grep -rl "count_trials\|run_promote_gate" tests/`):

- `record_trial_stat`: happy path, duplicate `run_id`, NaN-only P&L series
  (→ `trade_sharpe is None`, `n_trades == 0`).
- `count_trials` from seeded DB rows: totals, family grouping, NaN
  `trade_sharpe` rows excluded.
- Fail-closed: holdout fixture with one `NaN` `pnl_raw` (write the literal
  token `NaN` into the fixture JSON to prove the `json.loads` path) → DSR
  GateRun `passed=False`.
- Pass-open visibility: <2 sharpes → passes with the explanatory string.
- Backfill: tmp tree with 2 sidecars → 2 rows; second invocation → 0 new.

## Done criteria

- [ ] `uv run pytest -q`, `ruff check`, `ruff format --check`, `mypy src` all exit 0
- [ ] `uv run alembic upgrade head` applies 0011; downgrade/upgrade round-trips
- [ ] `grep -n "glob" src/fwbg_agents/orchestrator/trials.py` → no match (census is DB-only)
- [ ] `grep -n "honest-or-stricter" src/fwbg_agents/orchestrator/trials.py` → no match
- [ ] A NaN trade value can no longer pass the DSR check (test proves it)
- [ ] `scripts/backfill_trial_stats.py` exists and is idempotent (test proves it)
- [ ] No files outside the in-scope list modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back if:

- Plan 014 has NOT landed and `promote_gate.py` around lines 101–141 differs
  from the excerpt in a way that goes beyond 014's described changes.
- `runner.py`'s completion path has no access to a DB session where the
  sidecar is written — report the call chain rather than restructuring it.
- The migration collides with a concurrent `0011` revision (someone else
  landed one) — renumber only after confirming with the operator.
- Existing tests depend on `count_trials` scanning the filesystem in a way
  that isn't just fixture setup (i.e., production code passes paths, not
  sessions) — report before rewriting.

## Maintenance notes

- The census is now write-time: if a new run type is added (e.g. a future
  walk-forward revalidation), it must call `record_trial_stat` or it silently
  undercounts — reviewers should watch for new `execute_backtest` call sites.
- `GET /trials/summary`'s docstring says "recomputed on every call — it's a
  filesystem scan"; update that comment (it becomes a DB query).
- Deferred deliberately: making the <2-sharpes cold-start fail-closed (needs
  maintainer decision on cold-start UX), and counting fwbg grid breadth more
  precisely when `total_combinations` is absent (needs fwbg-side artifact
  change).
- The fwbg-dashboard has a TS port of the DSR math (fwbg-dashboard commit
  b36d0fb) — nothing here changes the formula, but if it ever does, the port
  must follow.
