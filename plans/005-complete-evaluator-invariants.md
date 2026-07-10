# Plan 005: Implement the plugin evaluator's missing invariants (NaN/inf, dtype)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dc84bd6..HEAD -- src/fwbg_agents/agents/plugin_evaluator.py src/fwbg_agents/orchestrator/plugin_contract.py`
> On mismatch with the excerpts below, STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: 004 (both touch the plugin-authoring quality gates; land 004 first)
- **Category**: bug
- **Planned at**: commit `dc84bd6`, 2026-07-10

## Why this matters

`PluginEvaluator` is the sole automated gate that promotes an LLM-authored
plugin AUTHORED→VERIFIED; a VERIFIED plugin is then registered with the fwbg
backend and used in real backtests. Its scenario runner's docstring promises
"the contract's three hard-coded invariants," but only **one** is
implemented (output-length parity). A plugin that returns correct-length but
all-NaN, infinite, or wrongly-typed output passes "verification". The gate
is largely cosmetic for output *quality*, which matters because the outputs
feed trading decisions.

## Current state

`src/fwbg_agents/agents/plugin_evaluator.py` — `_evaluate_scenario`
(lines 221–267):

```python
def _evaluate_scenario(compute, df, *, contract, scenario, params):
    """Run compute() and check the contract's three hard-coded invariants.
    ...
    """
    try:
        result = compute(df, **params)
    except Exception as exc:
        return [{... "invariant_violated": "compute_raised", ...}]

    errors: list[dict[str, Any]] = []

    # Invariant 1: length parity for outputs marked same_as_input.
    output_lengths = _output_lengths(result, contract)
    expected_len = len(df)
    for declared in contract.outputs:
        length = output_lengths[declared.name]
        if declared.length_invariant == "same_as_input" and length != expected_len:
            errors.append({... "invariant_violated": "length_mismatch", ...})

    return errors        # ← function ends here; invariants 2 and 3 absent
```

Error dicts have the shape
`{"scenario_name", "invariant_violated", "traceback", "ts"}` — new checks
must reuse it. `_output_lengths` (line 270) maps declared output names to
observed lengths; read it to see how outputs are extracted from `result`
(you will need the same extraction to inspect values).

The contract model lives in `src/fwbg_agents/orchestrator/plugin_contract.py`
(imported as `PluginContract` / `PluginContractScenario`) — read it first to
see what per-output metadata exists (e.g. a dtype or bounds field may or may
not already be declared; the checks below must key off what the contract can
express today, without inventing new required fields).

Existing tests: `tests/agents/test_plugin_evaluator.py` — e.g.
`test_evaluator_happy_path_passes_and_transitions_to_verified` (line 138)
and `test_evaluator_length_mismatch_stays_authored` (line 167). Model new
tests after the length-mismatch one: it builds a plugin whose compute
violates the invariant and asserts the plugin stays AUTHORED with an error
log.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Targeted tests | `uv run pytest tests/agents/test_plugin_evaluator.py -q` | all pass |
| Full suite | `uv run pytest -q` | all pass |
| Lint | `uv run ruff check src/ tests/` | exit 0 |

## Scope

**In scope**:
- `src/fwbg_agents/agents/plugin_evaluator.py`
- `src/fwbg_agents/orchestrator/plugin_contract.py` — ONLY if adding an
  *optional* per-output field (e.g. `allow_nan: bool = False`) proves
  necessary; prefer working with existing fields.
- `tests/agents/test_plugin_evaluator.py`

**Out of scope**:
- `contract_check` / implementer (that's plan 004's layer).
- Relaxing or changing Invariant 1.
- The evaluator's DB/state-transition logic (`_finalise_*`).

## Git workflow

- Branch: `advisor/005-complete-evaluator-invariants`
- Conventional commit: `fix(evaluator): enforce finite/dtype invariants on plugin outputs`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Read the contract model and output extraction

Read `plugin_contract.py` (output spec fields) and
`_output_lengths` (plugin_evaluator.py:270+) to learn how `result` is
shaped (Series? dict of Series? DataFrame columns?). Write down which
declared-output metadata exists. Do not guess.

### Step 2: Invariant 2 — finite values

After the Invariant-1 loop, for each declared output extract its values
(same access pattern `_output_lengths` uses) and append an error dict with
`"invariant_violated": "non_finite_output"` when the output contains NaN or
±inf. Implementation notes:
- Use `pandas.isna(...).any()` / `numpy.isinf(...)` on numeric arrays; skip
  the inf check for non-numeric dtypes.
- Leading NaN warm-up is legitimate for indicators (e.g. a 20-bar moving
  average has 19 NaNs). Reject only **all-NaN** outputs by default:
  `if series.isna().all()`. Flag partial-NaN outputs only if the contract
  has a field expressing "no NaNs allowed" (Step 1 tells you).

### Step 3: Invariant 3 — dtype sanity

Append `"invariant_violated": "wrong_dtype"` when a declared output's values
are not numeric/boolean (`pandas.api.types.is_numeric_dtype` or
`is_bool_dtype`) — unless the contract declares another type for it (again,
per Step 1). Object-dtype output from an indicator is always a bug.

### Step 4: Align the docstring

Update the `_evaluate_scenario` docstring to enumerate exactly the
invariants now implemented (1: length parity, 2: finite/non-all-NaN,
3: dtype) — no more, no less.

### Step 5: Run the evaluator fixtures

**Verify**: `uv run pytest tests/agents/test_plugin_evaluator.py -q` → all
existing tests pass. If a previously-passing fixture plugin now fails
verification, examine whether the fixture is genuinely violating (fix the
fixture) or the check is too strict (see STOP conditions).

### Step 6: Full suite + lint

**Verify**: `uv run pytest -q` and `uv run ruff check src/ tests/` → exit 0.

## Test plan

Model after `test_evaluator_length_mismatch_stays_authored` (line 167):
- `test_evaluator_all_nan_output_stays_authored` — compute returns
  correct-length all-NaN Series → plugin stays AUTHORED, error log contains
  `non_finite_output`.
- `test_evaluator_inf_output_stays_authored` — one `inf` in the output.
- `test_evaluator_object_dtype_stays_authored` — Series of strings.
- `test_evaluator_warmup_nans_pass` — leading-NaN indicator (e.g. rolling
  mean) → VERIFIED (guards against over-strictness).

## Done criteria

- [ ] `uv run pytest -q` exits 0 incl. the 4 new tests
- [ ] `grep -c "invariant_violated" src/fwbg_agents/agents/plugin_evaluator.py` ≥ 4 (compute_raised, length, non_finite, dtype)
- [ ] Docstring lists exactly the implemented invariants
- [ ] No files outside scope modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

- Step 1 reveals outputs are not pandas/numpy containers (e.g. plain lists) —
  the dtype check design needs a rethink; report the actual shapes.
- Real verified plugins exist in `data/plugins/` that would flip to failed
  under the new invariants — list them; the maintainer decides on
  re-verification policy before this lands.
- The contract model cannot distinguish "numeric indicator" from
  legitimately-non-numeric outputs (if any phase produces labels/strings) —
  report which phases break the assumption.

## Maintenance notes

- If the contract later grows explicit per-output dtype/bounds fields, these
  checks should read them instead of the built-in defaults.
- Reviewer: scrutinize the warm-up-NaN policy — too strict rejects every
  rolling indicator; too lax lets all-NaN garbage verify.
- Deferred (recorded, not planned): a look-ahead/causality invariant needs
  scenario-level shifted-input comparisons — a design of its own.
