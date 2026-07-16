# Plan 014: Freeze the holdout per lineage, budget gate attempts, and stop leaking holdout metrics to the Analyst

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 75123b0..HEAD -- src/fwbg_agents/orchestrator/promote_gate.py src/fwbg_agents/agents/runner.py src/fwbg_agents/agents/analyst.py src/fwbg_agents/config.py`
> (in fwbg-agents) and, for the fwbg work package,
> `git diff --stat 8ab08f7..origin/main -- src/fwbg/optimization/process.py src/fwbg/api/runs.py`
> (in fwbg, after `git fetch origin`).
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P0
- **Effort**: M
- **Risk**: MED (changes what the promote gate measures; promote rate will drop by design)
- **Depends on**: none (but land before plan 015 — both edit `promote_gate.py`)
- **Category**: bug (statistical validity) / direction
- **Planned at**: fwbg-agents commit `75123b0`, fwbg commit `8ab08f7` (origin/main), 2026-07-16

## Why this matters

The promote gate's 24-month holdout is supposed to be data "no iteration ever
saw", judged once. Today it is neither:

1. The holdout window is recomputed from `date.today()` on **every** gate run,
   while iteration backtests cap their data at `today - holdout_months`
   computed at **their** run time. There is no single frozen boundary.
2. Every gate failure writes full metrics (holdout Sharpe, DSR value, failure
   strings with numbers) into `promote_gate_results.json`, and the Analyst
   prompt inlines that file verbatim. A lineage may reiterate up to 12 times
   (`settings.reiterate_max_depth`), each time seeing exactly how it failed the
   holdout — so the loop can optimize against the holdout, which silently turns
   it into a validation set. The strategies most likely to promote become the
   ones that happened to fit this specific 24-month tail.
3. There is no limit on gate attempts per lineage, and `fail_count` is scoped
   to one strategy slug (child strategies get fresh sidecars), not the lineage.
4. Bonus integrity bugs in the fwbg engine that undermine the same gate:
   the date-window slice is inclusive on **both** ends
   (`df.loc[start:end]` — pandas label slicing), so the boundary day appears
   in both the in-sample runs and the holdout run; and `cost_multiplier`
   accepts 0/negative values, which would make the cost-stress run trade for
   free.

After this plan: one frozen data boundary per lineage, holdout attempts are
budgeted, the Analyst sees only pass/fail + attempts remaining (no metric
values), and the engine can't be fed a degenerate window or cost multiplier.

## Current state

### fwbg-agents (repo `/…/fwbg-agents`, branch off `develop`)

- `src/fwbg_agents/orchestrator/promote_gate.py` — the gate. Window is built
  per call (lines 167–168):

  ```python
  today = date.today().isoformat()
  holdout_start = _months_ago_iso(settings.holdout_months)

  specs: list[tuple[str, str, dict[str, Any]]] = [
      ("holdout", "promote_holdout", {"start_date": holdout_start, "end_date": today}),
      ("cost_stress", "promote_cost_stress", {"cost_multiplier": COST_STRESS_MULTIPLIER}),
  ]
  ```

  Sidecar write + per-slug fail count (lines 232–240 and 144–151):

  ```python
  passed = bool(runs) and all(r.passed for r in runs)
  fail_count = _fail_count(strategy) + (0 if passed else 1)
  result = PromoteGateResult(passed=passed, runs=runs, fail_count=fail_count, dsr=dsr, n_trials=n_trials)
  sidecar = strategy_dir(strategy.slug) / "promote_gate_results.json"
  ```

  ```python
  def _fail_count(strategy: Strategy) -> int:
      sidecar = strategy_dir(strategy.slug) / "promote_gate_results.json"
  ```

- `src/fwbg_agents/agents/runner.py:266-268` — iteration backtests reserve the
  holdout by capping `end_date` at run time:

  ```python
  # Reserve the most recent `holdout_months` as an unseen
  # holdout — the promote gate validates on it later.
  end_date=_months_ago_iso(settings.holdout_months),
  ```

- `src/fwbg_agents/agents/analyst.py:492-497` — the leak. The raw sidecar JSON
  goes into the prompt:

  ```python
  gate_path = strategy_dir(strategy.slug) / "promote_gate_results.json"
  promote_gate = (
      gate_path.read_text()
      if gate_path.is_file()
      else "(promote gate not yet run — no prior failure)"
  )
  ```

  and is passed as `promote_gate=promote_gate` into `_render_prompt` (~line 520).

- `src/fwbg_agents/persistence/models.py:157-159` — lineage linkage exists:
  `Strategy.parent_strategy_id` (self-FK, nullable). The lineage **root** is
  the ancestor with `parent_strategy_id IS NULL`.

- `src/fwbg_agents/config.py:172-191` — `holdout_months: int` (default 24) and
  `dsr_min` live in `Settings`. New settings go here, same `Field(...)` style
  with a `description`.

- Sidecar-file convention: per-strategy JSON artifacts live under
  `strategy_dir(slug)` (see `promote_gate.py` and `lifecycle.strategy_dir`).
  This plan adds one more sidecar at the **lineage root**'s dir.

- `run_promote_gate` is called from exactly one place:
  `src/fwbg_agents/orchestrator/recommendations.py:157`
  (`validate_and_apply`, `Promote` branch); a failed gate returns `None` and
  the strategy stays BACKTESTED.

### fwbg (repo `/…/fwbg` — **local checkout is on a stale feature branch;
25 commits behind `origin/main`. Run `git fetch origin` and branch from
`origin/main`.**)

- `src/fwbg/optimization/process.py:413-421` (same on origin/main) — inclusive
  slice:

  ```python
  if strategy.start_date or strategy.end_date:
      n_before = len(df)
      df = df.loc[strategy.start_date:strategy.end_date]
  ```

- `src/fwbg/api/runs.py` (origin/main line 97) — unvalidated multiplier:

  ```python
  cost_multiplier: Optional[float] = None
  ```

  Contrast the convention two fields down: `last_n_bars: Optional[int] = Field(default=None, ge=1)`.

## Commands you will need

| Purpose | Command (run in the repo it applies to) | Expected on success |
|---|---|---|
| agents tests | `uv run pytest -q` | all pass (~709 baseline) |
| agents lint | `uv run ruff check src tests && uv run ruff format --check src tests` | exit 0 |
| agents types | `uv run mypy src` | exit 0 |
| fwbg tests | `uv run pytest -q` | all pass |
| fwbg lint | `uv run ruff check src/ packages/` | exit 0 |

## Scope

**In scope** (the only files you should modify):

- fwbg-agents: `src/fwbg_agents/orchestrator/promote_gate.py`,
  `src/fwbg_agents/agents/runner.py` (only the `end_date=` reservation),
  `src/fwbg_agents/agents/analyst.py` (only the `promote_gate` rendering block),
  `src/fwbg_agents/orchestrator/lineage_boundary.py` (create),
  `src/fwbg_agents/config.py`, plus tests
  (`tests/orchestrator/test_promote_gate.py` or wherever the existing gate
  tests live — find with `grep -rl run_promote_gate tests/`).
- fwbg: `src/fwbg/optimization/process.py` (slice semantics),
  `src/fwbg/api/runs.py` (+ the CLI arg parsing in `src/fwbg/cli/main.py` if it
  forwards `cost_multiplier`), plus tests under `tests/`.

**Out of scope** (do NOT touch):

- `src/fwbg_agents/orchestrator/trials.py` and the DSR internals — plan 015.
- The criteria YAMLs under `data/criteria/` — thresholds are the maintainer's.
- `orchestrator/lifecycle.py` state-machine logic (the gate call site in
  `recommendations.py` keeps its exact contract: gate fail → `None`).
- fwbg's fold/CV logic beyond the two cited lines.

## Git workflow

- fwbg-agents: branch `advisor/014-lineage-holdout-policy` off `develop`.
- fwbg: `git fetch origin` first, then branch `advisor/014-gate-integrity`
  off `origin/main` (NOT off the stale local checkout).
- Conventional commits (`feat(...)`, `fix(...)`), no Claude/Anthropic
  references anywhere in commit messages or PR text.
- Do not push or open PRs unless the operator says so.

## Steps

### Step 1 (fwbg): make the date-window slice half-open and validated

In `src/fwbg/optimization/process.py`, change the slice so `end_date` is
**exclusive**: parse both dates with `pd.Timestamp` up front and slice
`df = df.loc[(df.index >= start) & (df.index < end)]` (handle the
one-side-only cases: only `start` → `df.index >= start`; only `end` →
`df.index < end`). Log line stays. A malformed date must raise a clear
`ValueError` naming the bad value (the API/CLI layer will surface it).
Document the half-open contract in the docstring of the function containing
the slice.

In `src/fwbg/api/runs.py` add `Field(default=None, gt=0)` to `cost_multiplier`
(import `Field` from pydantic if not present; match the `last_n_bars` line).
If `src/fwbg/cli/main.py` parses `--cost-multiplier`, reject `<= 0` there with
a clear error too.

**Verify**: `uv run pytest -q` → all pass; plus the new tests from the Test
plan below pass.

### Step 2 (fwbg-agents): create the lineage boundary helper

New file `src/fwbg_agents/orchestrator/lineage_boundary.py`:

- `async def lineage_root(session, strategy) -> Strategy` — walk
  `parent_strategy_id` until `None` (guard against cycles with a visited-set;
  on a cycle, log an error and return the last non-repeated ancestor).
- `def boundary_path(root_slug: str) -> Path` — `strategy_dir(root_slug) / "lineage_boundary.json"`.
- `async def get_or_freeze_boundary(session, strategy) -> str` — returns the
  frozen ISO date `B`. If the root's `lineage_boundary.json` exists, read
  `{"data_end": "..."}` from it; otherwise write it with
  `data_end = _months_ago_iso(settings.holdout_months)` evaluated **now** and
  return that. File writes follow the sidecar pattern in
  `promote_gate.py:238-240` (mkdir parents, `write_text`, JSON).

Semantics: `B` is the exclusive upper bound of in-sample data for the whole
lineage, and the inclusive lower bound of the holdout.

**Verify**: `uv run pytest -q tests/ -k lineage_boundary` → new unit tests pass
(see Test plan).

### Step 3 (fwbg-agents): use the frozen boundary in iteration runs and the gate

- `runner.py:268`: replace `end_date=_months_ago_iso(settings.holdout_months)`
  with the lineage boundary `B` (the runner has the strategy + session in
  scope at that call site — check the enclosing function signature; if the
  session is not available there, resolve `B` in the caller and thread it
  through as a parameter). Every iteration backtest of every generation now
  ends at the same frozen `B`.
- `promote_gate.py:167-172`: holdout spec becomes
  `{"start_date": B, "end_date": date.today().isoformat()}`. With fwbg's
  half-open slice from Step 1, in-sample is `[..., B)` and holdout is
  `[B, today)` — zero overlap. Keep the cost-stress spec unchanged.
- Keep `_months_ago_iso` import only where still used.

**Verify**: `uv run pytest -q` → all pass; existing promote-gate tests updated
to the frozen-boundary behavior.

### Step 4 (fwbg-agents): lineage-scoped fail count + attempt budget

- New setting in `config.py`:
  `promote_max_attempts: int = Field(default=3, description="Maximum promote-gate attempts per lineage; once reached, further Promote recommendations fail the gate without running and the Analyst is told the budget is exhausted.")`
- Move the fail count to the lineage root: `_fail_count` reads/writes
  `promote_gate_results.json` under the **root** slug's dir (resolve via
  `lineage_root`). Child strategies therefore share one counter.
  (The per-child sidecar is still written for the run's own artifacts — only
  the counter lives at the root. Simplest implementation: keep writing the
  full `PromoteGateResult` sidecar at the root slug's dir and stop writing
  per-child copies; the Analyst rendering in Step 5 reads the root sidecar.)
- At the top of `run_promote_gate`: if the lineage fail count `>=
  settings.promote_max_attempts`, do not run any backtest; return a
  `PromoteGateResult(passed=False, runs=[], fail_count=<count>, ...)` and emit
  a `promote_gate_failed` run event with `reason="attempt budget exhausted"`.

**Verify**: new test: two children of one root; a gate failure recorded via
child A is visible as `fail_count=1` when child B runs the gate; after
`promote_max_attempts` failures the gate short-circuits (assert the fwbg
client/runner was NOT called — follow the mock pattern of the existing
promote-gate tests).

### Step 5 (fwbg-agents): Analyst sees pass/fail only

In `analyst.py:492-497`, replace the raw `gate_path.read_text()` with a
redacted rendering built from the parsed sidecar (now at the lineage root):

```
promote gate: FAILED (attempt 2 of 3)
  holdout: failed
  cost_stress: passed
  dsr: failed
```

Rules: per-run `label` + `passed` only. **No metric values, no failure
strings, no DSR number.** Include `attempt N of M`. When no sidecar exists,
keep the current "(promote gate not yet run — no prior failure)" text. Put the
rendering in a small pure function (e.g. `_render_promote_gate_summary(data:
dict, max_attempts: int) -> str`) so it's unit-testable. Full metrics remain
in the sidecar for humans/dashboard — only the prompt is redacted.

**Verify**: unit test feeds a `PromoteGateResult`-shaped dict containing a
numeric `dsr` and numeric failure strings; assert the rendered string contains
`holdout` and `failed` but does NOT contain any digit-bearing metric (e.g.
assert `"dsr=" not in rendered` and no substring matching `r"\d+\.\d+"`).

### Step 6: full gates

**Verify** (fwbg-agents): `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src` → all exit 0.
**Verify** (fwbg): `uv run pytest -q && uv run ruff check src/ packages/` → exit 0.

## Test plan

- fwbg, new tests (model after the existing date-window tests referenced from
  `tests/test_api_data.py`; if a better exemplar exists under
  `tests/optimization/`, use it):
  - half-open slice: synthetic daily `DatetimeIndex`; `start=D0,end=D5` yields
    exactly D0..D4; only-start and only-end variants; malformed date → clear
    `ValueError`.
  - `cost_multiplier=0` and `-1` → 422 from the API model (use the existing
    request-model test pattern in `tests/test_api_run_spawn.py`).
- fwbg-agents, new tests:
  - `lineage_boundary`: freeze-once (two calls return the same date across
    different "today"), root resolution over a 3-deep chain, cycle guard.
  - gate: frozen window used in the holdout spec (assert on the runner mock's
    kwargs); lineage-shared fail count; budget short-circuit.
  - analyst rendering redaction (Step 5).
- Existing promote-gate tests: update expectations, do not delete assertions.

## Done criteria

- [ ] fwbg-agents: `uv run pytest -q`, `ruff check`, `ruff format --check`, `mypy src` all exit 0
- [ ] fwbg: `uv run pytest -q`, `ruff check src/ packages/` exit 0
- [ ] `grep -n "date.today" src/fwbg_agents/orchestrator/promote_gate.py` shows `today` used only as the holdout **end**, not its start
- [ ] `grep -rn "_months_ago_iso(settings.holdout_months)" src/fwbg_agents/agents/runner.py` → no match (replaced by frozen boundary)
- [ ] Analyst prompt rendering contains no metric values from the gate sidecar (unit test enforces)
- [ ] No files outside the in-scope list modified (`git status` in both repos)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The excerpts in "Current state" don't match the live code (drift).
- `runner.py`'s backtest call site cannot reach a DB session and threading `B`
  through requires touching more than two function signatures — report the
  call-chain instead of refactoring it.
- Changing the fwbg slice to half-open breaks existing fwbg tests that
  explicitly assert inclusive-end behavior — that means someone depends on the
  old semantics; report which tests.
- You find a second call site of `run_promote_gate` besides
  `recommendations.py:157`.
- Existing strategy dirs in production data contain per-child
  `promote_gate_results.json` files with nonzero `fail_count` — decide nothing
  about migrating them; report and ask (default assumption: old counters are
  NOT migrated to roots).

## Maintenance notes

- Plan 015 (durable trial stats) edits `promote_gate.py` too — land this plan
  first; 015 must count budgeted-but-skipped gate attempts as trials.
- The frozen boundary means long-lived lineages backtest on increasingly stale
  in-sample data — that is the intended trade (validity over recency). If the
  maintainer later wants rotation instead of freezing, `lineage_boundary.json`
  is the single place to change.
- Reviewer should scrutinize: the half-open slice change in fwbg (every
  existing caller of date windows sees it) and that the Analyst redaction
  didn't accidentally drop the "promote gate not yet run" default text.
- Deferred: counting gate attempts into the DSR `n_trials` (goes into plan
  015's durable stats), and any UI for the lineage boundary.
