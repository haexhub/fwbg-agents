# Plan 023: Remove temporal leakage and separate development from final evidence

> **Executor**: Work in a disposable `fwbg` branch, one work package at a time. Run every gate and stop on unexplained score changes. The reviewer maintains the index.
>
> **Drift check**: `git -C ../fwbg diff --stat f76ef8f..HEAD -- src/fwbg/optimization/process.py src/fwbg/optimization/nested_cv.py src/fwbg/plugins/fwbg-core/indicators/cusum_events/__init__.py tests`.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: 014, then 015
- **Category**: bug (statistical validity)
- **Planned at**: fwbg `f76ef8f`, 2026-07-20

## Why this matters

Walk-forward folds cease to be out-of-sample when their results select indicator variants and unified settings. Repeated agent iterations amplify that adaptive reuse. Meta-label OOF predictions additionally train on future blocks, and CUSUM warm-up uses full-series statistics.

## Current state

- `src/fwbg/optimization/process.py:315-363` ranks variants by walk-forward `test_pnl`; `:638-650` derives/re-simulates unified settings; `:775-784` reports those folds.
- `src/fwbg/optimization/nested_cv.py:218-251` uses ordinary `KFold(shuffle=False)`, whose training set includes blocks after validation.
- `src/fwbg/plugins/fwbg-core/indicators/cusum_events/__init__.py:108-115` fills warm-up from complete-series mean/std.
- Plan 014 owns the immutable lineage boundary and holdout-attempt budget; do not create a competing boundary.

## Commands

- Focused: `cd ../fwbg && uv run pytest tests/test_meta_labeling.py tests/test_lookahead_bias.py tests/test_no_bias_in_system.py tests/test_robust_validation.py -q` → pass.
- Full/lint: `cd ../fwbg && uv run pytest -q && uv run ruff check src packages` → pass.

## Scope

In scope: the three modules above, explicit validation-role models/schema documentation, and regression tests. Out of scope: threshold tuning, profitability changes, portfolio allocation, or bypassing Plans 014/015.

## Steps

1. Add fixed-seed characterization fixtures recording fold boundaries and result roles; do not lock current profitability as the acceptance target.
2. Replace meta-label KFold with expanding forward splits. Require `max(train)<min(validation)`, purge/embargo by maximum label horizon, represent the initial unavailable OOF region as missing, and exclude it from fitting. Test indices directly.
3. Remove global CUSUM warm-up estimates. Keep missing values or use expanding history only. Add prefix invariance: results on `[0:T]` equal the prefix of `[0:T+n]`, including NaN positions.
4. Add versioned roles `development_walk_forward` and `final_holdout`. Compatibility serialization is allowed, but selection folds must never be named/treated as final OOS.
5. Keep variant selection on development folds and freeze strategy/plugin hashes and parameters before Plan 014's holdout. Final-holdout data is available only to the promote gate and never to variant/config selection.
6. Add integration coverage proving development selects a variant while final data is not read before artifact freeze; a repeat evaluation must consume the lineage attempt budget.
7. Update affected API/result docs. If dashboard/agents contracts must change, STOP with an exact old/new schema for a reviewed follow-up.

## Done criteria

- [ ] Meta-label validation is strictly forward and embargoed.
- [ ] CUSUM passes prefix invariance.
- [ ] Variant selection reads development data only.
- [ ] Artifact hashes/data roles exist before final evaluation.
- [ ] Final metrics never feed optimization or detailed Analyst advice.
- [ ] Focused/full tests and Ruff pass.

## STOP conditions

- Label horizon cannot be derived from the active exit/timeout configuration.
- Plan 014 is unavailable in the branch.
- A consumer cannot be migrated without expanding reviewed scope.
- Tests expose another unexplained future-data dependency.

## Maintenance

Every new feature should pass prefix invariance; every optimizer must declare its input data role. Metrics influenced by selection must not be labeled OOS.
