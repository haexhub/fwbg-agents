# Plugin Constitution

> The non-negotiable principles every fwbg plugin/indicator MUST satisfy.
> Adapted from spec-kit's `constitution.md`. Loaded by the speckit workflow and
> injected into the authoring agents. Keep in sync with `prompts/plugin_authoring.md`
> (the detailed SDK conventions) and the `fwbg_sdk` base classes.

## I. One capability per plugin — no duplicates

A plugin implements exactly **one** capability, stated in a single sentence
(the spec's `capability` line). Before authoring a new plugin, its capability
MUST be matched against the `capability` of every existing plugin spec. If an
existing plugin already satisfies it, **reuse that plugin — do not author a new
one.** Near-duplicates under different names are the failure this constitution
exists to prevent.

## II. Canonical vocabulary

Two names describe a plugin; use them consistently:

- **kind** (the category, singular) — one of:
  `indicator`, `model`, `exit_strategy`, `risk_management`, `entry_modifier`,
  `preprocessing`, `feature_selection`, `data_loading`.
  This is the `spec.kind` / `contract.kind` / catalog vocabulary.
- **phase** (the pipeline stage, plural) — one of:
  `data_loading`, `preprocessing`, `indicators`, `feature_selection`,
  `exit_strategies`, `risk_management`, `labeling`, `model`, `validation`
  (the `fwbg_sdk.base.PluginPhase` enum).

A capability the Analyst labels `filter(s)` maps to `risk_management`.

## III. Contract before code

Every plugin ships a `contract.yaml` (schema: `PluginContract`) declaring
`inputs`, `outputs`, `params`, `invariants`, and `test_scenarios`. The contract
is the LLM-bypass-resistant validator the Evaluator checks before a plugin may
transition AUTHORED → VERIFIED. An `indicator` MUST declare at least one
invariant.

## IV. No lookahead bias (MANDATORY for indicators)

Feature columns MUST be shifted by one bar before being returned
(`shift_features(...)`). Every indicator MUST include a no-lookahead test
proving `df[col].iloc[i]` never depends on `df.iloc[i+1:]`. Use `safe_divide`
for all divisions.

## V. Testable by construction

- **Minimum 3 pytest tests** per plugin (the Evaluator counts and rejects fewer).
- Names are `test_<behaviour>`, snake_case, descriptive.
- Include the no-lookahead test (indicators) and at least one non-default
  parameter-variation test. Edge cases (empty/single-row/all-NaN/constant)
  are encouraged and belong in the spec's Edge Cases.

## VI. Naming & identity

- `name` (class attr) == directory slug, snake_case.
- Class name is PascalCase-slug + phase suffix
  (`Indicator`/`Preprocessor`/`Selector`/`RiskManager`).
- Feature columns are snake_case, prefixed with the slug, and MUST exactly
  match the columns `compute()`/`transform()` actually produce.

## VII. Parameters are declared

Params appear in `get_default_params()` and (preferably) `get_param_schema()`
with non-empty descriptions. The spec records each param's name, type,
default, and description; the plan/contract add min/max/step/choices.
