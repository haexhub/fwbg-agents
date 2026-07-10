# Plan 001: Enforce stop-loss and fail-closed criteria gates in deterministic code

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dc84bd6..HEAD -- src/fwbg_agents/orchestrator/strategy_validator.py src/fwbg_agents/orchestrator/lifecycle.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug / security
- **Planned at**: commit `dc84bd6`, 2026-07-10

## Why this matters

The repo's CLAUDE.md declares two critical safety rules: "Stop-loss is
mandatory for every order, paper or live" and criteria-gated promotion from
BACKTESTED to PAPER_TRADING. Today the stop-loss rule is enforced **only by
LLM prompt text** (`src/fwbg_agents/agents/prompts/translator.md`), never by
deterministic code — a strategy whose `exit_strategies` contains only a
take-profit passes validation. And the criteria gate **fails open**: when no
criteria YAML exists for an asset class, `check_backtest_criteria` returns
`(True, [])`, so on a fresh checkout the money-adjacent promotion gate passes
unconditionally. Finally, criteria evaluation calls a bare `float(val)` on
metric values, so one malformed metric turns a promotion attempt into an
unhandled `ValueError` instead of a clean rejection.

## Current state

- `src/fwbg_agents/orchestrator/strategy_validator.py` — deterministic
  strategy.json validator. `_check_exit_strategies` (lines 166–184) checks
  only that entries are dicts with `name` (str) and `params` (dict) and, when
  a catalog is present, that `name` is a known exit-strategy slug. There is
  no check that any entry is a stop-loss:

  ```python
  # strategy_validator.py:166-175
  def _check_exit_strategies(items: Any, *, catalog: PluginCatalog | None) -> None:
      if not isinstance(items, list) or not items:
          raise StrategyValidationError("exit_strategies must be a non-empty list")
      for i, item in enumerate(items):
          if not isinstance(item, dict):
              raise StrategyValidationError(f"exit_strategies[{i}] must be an object")
          if "name" not in item or not isinstance(item["name"], str):
              raise StrategyValidationError(f"exit_strategies[{i}].name is required (str)")
          if "params" not in item or not isinstance(item["params"], dict):
              raise StrategyValidationError(f"exit_strategies[{i}].params is required (object)")
  ```

- `src/fwbg_agents/orchestrator/lifecycle.py` — lifecycle state machine (the
  repo's central abstraction; all transitions go through it).
  `check_backtest_criteria` (docstring around lines 133–139) documents the
  fail-open behavior:

  ```python
  # lifecycle.py:133-139 (docstring excerpt + code)
  #  Pass-through behaviour: if no YAML exists for the asset class, returns
  #  `(True, [])`. The calibrator seeds defaults but M2 still needs to be
  #  usable on a fresh checkout.
  path = _criteria_path(asset_class)
  if not path.is_file():
      return True, []
  ```

  The unguarded float casts are at lines 156, 167, and 179, all of the form
  `_eval_comparator(str(expr), float(val))`.

  The guard that consumes this is `_guard_strategy_backtested_to_paper`
  (lines 200–208); it raises `InvalidTransitionError` on failures.

- Conventions: validation failures raise `StrategyValidationError`
  (strategy_validator.py) or `InvalidTransitionError` (lifecycle.py) with
  actionable messages. Tests are plain pytest with `asyncio_mode = "auto"`.
  Exemplar tests: `tests/orchestrator/test_strategy_validator.py`
  (`test_empty_exit_strategies_fails` at line 131) and
  `tests/orchestrator/test_lifecycle.py`.

## Commands you will need

| Purpose   | Command                                        | Expected on success |
|-----------|------------------------------------------------|---------------------|
| Install   | `uv sync`                                      | exit 0              |
| Tests     | `uv run pytest -q`                             | all pass            |
| One file  | `uv run pytest tests/orchestrator/test_strategy_validator.py -q` | all pass |
| Lint      | `uv run ruff check src/ tests/`                | exit 0              |

## Scope

**In scope** (the only files you should modify):
- `src/fwbg_agents/orchestrator/strategy_validator.py`
- `src/fwbg_agents/orchestrator/lifecycle.py`
- `tests/orchestrator/test_strategy_validator.py`
- `tests/orchestrator/test_lifecycle.py`
- `tests/orchestrator/test_paper_flow.py` — Step 3 updates tests that relied on the fail-open path

**Out of scope** (do NOT touch, even though they look related):
- `src/fwbg_agents/agents/prompts/translator.md` — the prompt guidance stays;
  this plan adds the deterministic backstop, it does not move the rule.
- `src/fwbg_agents/orchestrator/paper_flow.py`, `api/strategies.py` — the
  paper→live human gate is correctly enforced already.
- The calibrator (`agents/calibrator.py`) — criteria seeding is its job and
  works.

## Git workflow

- Branch: `advisor/001-enforce-deterministic-safety-gates`
- Conventional commits, e.g. `fix(validator): require a stop-loss exit strategy`
  (style matches `git log`, e.g. "fix(auto_runner): retry add_indicator chain…")
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Discover the canonical stop-loss identifier(s)

The fwbg exit-strategy vocabulary lives in the fwbg repo / live catalog, not
here. Find which exit-strategy slugs or params denote a stop-loss:

- `grep -rn "exit_strategies" tests/ scripts/ --include="*.py" -A3 | grep -i "name"` — collect the slug values used in fixtures.
- `grep -rn "stop" src/ tests/ prompts/ --include="*.md" --include="*.py" -il` — read hits in `prompts/` and test fixtures.
- If a sibling fwbg checkout exists (`ls ../fwbg`), also:
  `grep -rn "stop_loss\|stop-loss" ../fwbg/src --include="*.py" -l | head`.

Record the identified slug set (expected shape: something like
`{"stop_loss", "trailing_stop", ...}`) as a module-level frozenset
`_STOP_LOSS_EXIT_NAMES` in `strategy_validator.py`, with a comment naming the
source of truth you derived it from.

**Verify**: the frozenset members all appear in at least one fixture or
prompt (`grep -rn "<slug>" tests/ prompts/` → at least one hit each).

### Step 2: Require a stop-loss entry in `_check_exit_strategies`

At the end of `_check_exit_strategies` (after the per-item loop), add:

```python
    if not any(
        isinstance(item, dict) and item.get("name") in _STOP_LOSS_EXIT_NAMES
        for item in items
    ):
        raise StrategyValidationError(
            "exit_strategies must include a stop-loss entry "
            f"(one of {sorted(_STOP_LOSS_EXIT_NAMES)}); stop-loss is mandatory "
            "for every strategy (see CLAUDE.md critical safety rules)"
        )
```

**Verify**: `uv run pytest tests/orchestrator/test_strategy_validator.py -q`
→ expect failures ONLY in fixtures that lack a stop-loss; fix those fixtures
by adding a stop-loss entry (they represent valid strategies, so they must
comply). If more than ~5 unrelated tests fail, STOP.

### Step 3: Make missing criteria fail closed

In `check_backtest_criteria` (lifecycle.py), replace the pass-through:

```python
    path = _criteria_path(asset_class)
    if not path.is_file():
        return False, [
            f"no criteria defined for asset class {asset_class!r}; "
            "run POST /calibrate to seed criteria before promoting to paper"
        ]
```

Update the docstring's "Pass-through behaviour" paragraph accordingly.

**Verify**: `uv run pytest tests/orchestrator/test_lifecycle.py tests/orchestrator/test_paper_flow.py -q`
→ tests that relied on the fail-open path will fail; update them to seed a
criteria YAML first (see how `tests/orchestrator/test_lifecycle.py` writes
criteria files elsewhere in the file) or to assert the new failure message.

### Step 4: Guard the float casts

Add a small helper in lifecycle.py (module level, near `_eval_comparator`):

```python
def _metric_float(val: Any) -> float | None:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
```

(`import math` at the top.) Replace each of the three `float(val)` call sites
(lines 156, 167, 179) so a `None` result appends a failure (e.g.
`f"{metric}={val!r} is not numeric"`) instead of raising. Mirror the existing
`missing metric` failure style.

**Verify**: add a test in `test_lifecycle.py` passing
`metrics={"sharpe": "n/a"}` → transition fails with the "is not numeric"
message, no exception escapes.

### Step 5: Full suite + lint

**Verify**: `uv run pytest -q` → all pass. `uv run ruff check src/ tests/` → exit 0.

## Test plan

New tests (model after `tests/orchestrator/test_strategy_validator.py:131`
`test_empty_exit_strategies_fails`):
- `test_exit_strategies_without_stop_loss_fails` — valid payload minus any
  stop-loss entry → `StrategyValidationError` mentioning "stop-loss".
- `test_exit_strategies_with_stop_loss_passes` — happy path.
- In `test_lifecycle.py`: `test_missing_criteria_yaml_blocks_promotion` —
  no YAML seeded → `InvalidTransitionError` with the "run POST /calibrate"
  message; `test_non_numeric_metric_fails_cleanly` (Step 4).

## Done criteria

- [ ] `uv run pytest -q` exits 0; the 4 new tests exist and pass
- [ ] `grep -n "_STOP_LOSS_EXIT_NAMES" src/fwbg_agents/orchestrator/strategy_validator.py` → 2+ matches (definition + use)
- [ ] `grep -n "return True, \[\]" src/fwbg_agents/orchestrator/lifecycle.py` → no match on the missing-file path
- [ ] No files outside the in-scope list modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- Step 1 cannot identify an unambiguous stop-loss slug set (the fwbg
  vocabulary may use per-order `sl` params instead of an exit-strategy slug —
  if so, the right check may live on order params, which is a design decision
  the maintainer must make).
- Step 3 breaks more than ~10 tests — the fail-open path may be load-bearing
  for flows this plan didn't map.
- The code at the cited lines doesn't match the excerpts (drift).

## Maintenance notes

- If fwbg adds new stop-loss-family exit strategies, `_STOP_LOSS_EXIT_NAMES`
  must be extended — consider deriving it from the live catalog later.
- Reviewer should scrutinize: whether fixtures updated in Step 2 still
  represent realistic strategies; the Step 3 error message must name the
  exact calibrate endpoint.
- Deferred: enforcing SL at the fwbg order level (belongs in the fwbg repo's
  pre-trade validators; this plan hardens the strategy-config layer only).
