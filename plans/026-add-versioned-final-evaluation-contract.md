# Plan 026: Add a versioned, selection-free final evaluation contract

> **Executor instructions**: This plan spans `fwbg` (including `fwbg-sdk`) and
> `fwbg-agents`. Follow the repository and rollout order exactly. The purpose is
> not to rename the current holdout run; it is to create a separate execution
> path that cannot optimize on final data. Run every verification gate. If a
> final evaluator would need to reuse a function that performs selection,
> mutation, or full-window calibration, stop and split/purify that function
> instead of adding a mode flag deep inside it. Update the index only after both
> PRs, the migration, backfill, and an end-to-end evidence run are complete.
>
> **Drift check (run first)**:
>
> Run the first command from the fwbg repository and the second from the
> fwbg-agents repository (do not assume the repositories are sibling paths):
>
> ```bash
> git diff --stat 22ff2be..HEAD -- packages/fwbg-sdk src/fwbg tests
> git diff --stat 3371802..HEAD -- pyproject.toml uv.lock src/fwbg_agents tests scripts
> ```
>
> If an in-scope file changed, compare it with "Current state". A semantic
> mismatch, especially around `process_symbol`, `promote_gate`, lineage
> boundaries, run adoption, or migration head, is a STOP condition.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plans 014, 015, and the mergeable leakage subset of Plan 023
- **Category**: correctness, architecture, migration, tests
- **Planned at**: plan-index commit `371eb50`, 2026-07-20; audited source
  snapshots: fwbg `22ff2be`, fwbg-agents `3371802`

## Why this matters

The current promote "holdout" passes `[B, today)` into the ordinary fwbg
optimization command. Inside that window fwbg expands indicator variants,
chooses the best one, merges fold settings, and recalibrates risk from the
resulting trades. The reported holdout is therefore another selection window,
not independent final evidence. The cost-stress run also omits the lineage end
date and can inspect data outside the intended final boundary.

The attempt budget is a JSON sidecar updated after work completes. Concurrent
calls or a crash can expose final data without durably consuming an attempt.
Run adoption compares only status and strategy name, so a retry can attach to a
run with different dates, costs, assets, or evaluation semantics. Together,
these gaps can make a strategy appear better than it is and allow agents to
learn from nominally private evidence.

This plan introduces a small shared contract in `fwbg-sdk`, an immutable
development candidate manifest, a dedicated selection-free final evaluator,
canonical request fingerprints, durable atomic attempt claims, and explicit
public/private projections. Development remains a walk-forward search. Final
evaluation applies one frozen candidate once.

## Terminology and fixed contract

Use these terms consistently in code, API, artifacts, logs, and tests:

- **development evaluation**: walk-forward optimization on data strictly before
  lineage boundary `B`; it may select variants and parameters.
- **candidate manifest**: immutable schema-versioned output of a successful
  development evaluation, containing every decision needed to replay the
  candidate without selection.
- **final evaluation**: one baseline and one cost-stress scenario on `[B, E)`
  using the same candidate manifest. It may fit a model on data before `B` with
  frozen features/hyperparameters; it may not select, tune, merge, or
  recalibrate from final outcomes.
- **attempt**: one atomic claim that permits the baseline and cost-stress
  scenarios for one candidate. Claiming consumes the ordinal even if the
  process crashes or validation fails.
- **public projection**: pass/fail, a closed coarse check category
  (`holdout_criteria`, `cost_stress`, `dsr`, or `execution`), attempt ordinal,
  and artifact hash only. It contains no metric/comparator/value/direction.
  This is all an Analyst/Researcher/Translator may see.
- **private result**: metrics, trades, run IDs, errors, and diagnostics. It is
  persisted for human audit but never injected into a strategy-generating or
  strategy-improving agent prompt.

The additive API object is exactly:

```json
{
  "schema_version": 1,
  "role": "final_holdout",
  "lineage_id": "stable lineage-root identifier",
  "attempt_id": "stable final-attempt UUID",
  "scenario": "baseline",
  "source_run_id": "development run ID",
  "source_artifact_sha256": "64 lowercase hexadecimal characters"
}
```

`evaluation` may be absent only for manual/legacy runs, which are marked
`unclassified` and are never eligible as development artifacts or promotion
evidence. Agents-created runs must always send it. Unknown schema versions,
roles, scenarios, or illegal field combinations fail before a process starts.
For `development_walk_forward`, `attempt_id`, `source_run_id`, and
`source_artifact_sha256` are omitted. Sending them as `null` is invalid so
canonical fingerprints have only one representation.

## Current state

### fwbg API and optimizer

- `src/fwbg/api/runs.py:88-98` accepts strategy, universe, date window, and cost
  multiplier as unrelated optional fields. `start_run()` returns only job ID,
  status, strategy name, and PID (`runs.py:184-189`). There is no semantic
  version or request fingerprint.
- `src/fwbg/optimization/process.py:443-456` slices the loaded frame to the
  requested window. A holdout request therefore makes the holdout the complete
  input to all following optimization steps.
- `process.py:577-604` expands indicator variants and selects the best variant
  inside that input window.
- `process.py:655-733` derives a majority configuration, merges unified
  settings, and re-simulates them on the same window.
- `process.py:774-787` invokes the risk-management plugin using outcomes from
  that same window.
- `process.py:1086-1125` serializes only part of the effective candidate. The
  result lacks enough information to replay all feature, exit, variant, and
  risk choices without re-selection.
- `packages/fwbg-sdk` is a typed, dependency-light shared package but currently
  has no evaluation contract or canonical hashing helper.

### fwbg-agents

- `src/fwbg_agents/orchestrator/promote_gate.py:201-222` runs ordinary
  backtests for holdout and cost stress. Only holdout gets explicit start/end;
  cost stress gets just `cost_multiplier`.
- `promote_gate.py:165-178,191-199` reads a cumulative `fail_count` from a JSON
  sidecar. The claim/update is not transactional and only failed results
  consume the visible count.
- `orchestrator/lineage_boundary.py:67-100` persists the frozen `B` boundary in
  another sidecar. It is useful but is not atomic and must cease being an
  authority once the database policy exists.
- `agents/runner.py:462-525` adopts any running job with the same strategy name;
  it does not compare the universe, dates, cost multiplier, role, scenario, or
  source artifact.
- `tools/fwbg_client.py:125-154` and `:229-233` use untyped dictionaries at the
  run boundary.
- `orchestrator/recommendations.py:154-171` permits promotion when no fwbg
  client was supplied, explicitly skipping the promote gate. Final evidence
  must instead fail closed.
- Migration `0011_trial_stat.py` is the current audited head. It demonstrates
  the repository's Alembic style and durable trial-stat model.
- `pyproject.toml:41-43` tracks `fwbg-sdk` by the moving `main` branch, even
  though `uv.lock` resolves one commit. Cross-repository contract deployment
  needs an immutable SDK ref.

## Commands you will need

| Repository | Purpose | Command | Expected on success |
|---|---|---|---|
| fwbg SDK | Focused tests | `uv run pytest packages/fwbg-sdk/tests/test_evaluation.py -q` | all pass |
| fwbg | Evaluation tests | `uv run pytest tests/test_evaluation_contract.py tests/test_evaluation_manifest.py tests/test_final_holdout.py tests/test_api_run_start_validation.py tests/test_api_run_spawn.py -q` | all pass; create the first three and extend the existing API files |
| fwbg | Full tests | `uv run pytest` | all pass, apart from already documented skips |
| fwbg | Lint | `uv run ruff check src packages tests` | exit 0 |
| fwbg | Format | `uv run ruff format --check src packages tests` | exit 0 |
| agents | Migration | `uv run alembic upgrade head` | upgrades through the new revision |
| agents | Focused tests | `uv run pytest tests/orchestrator/test_promote_gate.py tests/agents/test_runner.py tests/tools/test_fwbg_client.py tests/persistence -q` | all pass |
| agents | Full tests | `uv run pytest` | all pass except any explicitly proven pre-existing worktree fixture issue |
| agents | Lint | `uv run ruff check src tests scripts` | exit 0 |
| agents | Format | `uv run ruff format --check src tests scripts` | exit 0 |
| agents | Types | `uv run mypy src` | exit 0 |

Use temporary databases and synthetic OHLCV fixtures. Final-evaluation tests
must never read current production data.

## Scope

### In scope

`fwbg`:

- `packages/fwbg-sdk/src/fwbg_sdk/evaluation.py`, its export and tests
- `src/fwbg/api/runs.py`, run registry/detail/list response construction
- CLI argument plumbing for the evaluation envelope and manifest reference
- `src/fwbg/optimization/candidate_manifest.py` (new)
- `src/fwbg/optimization/final_evaluation.py` (new)
- `src/fwbg/results/evaluation_artifacts.py` (new content-addressed store)
- narrow extractions from `src/fwbg/optimization/process.py` and its helpers
  only where needed to share pure data preparation/simulation
- evaluation API/optimizer tests and user-facing API documentation

`fwbg-agents`:

- `pyproject.toml`, `uv.lock` for an immutable SDK ref
- `src/fwbg_agents/tools/fwbg_client.py`
- `src/fwbg_agents/agents/runner.py`
- `src/fwbg_agents/orchestrator/promote_gate.py`,
  `lineage_boundary.py`, and a new `evaluation_evidence.py`
- `src/fwbg_agents/orchestrator/recommendations.py` and Analyst artifact/prompt
  projection code
- `src/fwbg_agents/persistence/models.py` and the next Alembic migration
- a one-shot backfill script under `scripts/`
- focused API, persistence, runner, prompt, migration, and E2E tests

### Out of scope

- Dashboard visualization of private evidence.
- Changing promote thresholds, DSR mathematics, cost multiplier, or the number
  of allowed attempts. Persist their current values; do not recalibrate them in
  this plan.
- Reusing final results to improve a candidate. A failed candidate may produce
  a public failed-check label, but no metrics or directional advice.
- Executing historical source snapshots after strategy/plugin code changes.
  The contract is hash-bound: a digest mismatch requires a new development
  evaluation. It does not run stale code from an artifact.
- General event sourcing for all sidecars. Only lineage evaluation policy,
  candidate artifacts, attempts, and projections move to durable storage here.
- Dashboard/manual runs without an evaluation envelope. They remain
  `unclassified` and cannot count as evidence.

## Git workflow and merge boundaries

- fwbg SDK branch/PR: `advisor/026-evaluation-sdk`, based on the Plan 023
  leakage branch after it is rebased/merged.
- fwbg server branch/PR: `advisor/026-final-evaluation-server`, based on the
  merged SDK PR. Keeping these as two PRs makes the stated release boundary
  executable.
- agents branch: `advisor/026-durable-evaluation-evidence`, based on Plans 014
  and 015.
- Conventional commits, for example `feat(sdk): define evaluation contract`,
  `feat(validation): add frozen final evaluator`, and
  `fix(promote-gate): claim attempts atomically`.
- Merge/publish the SDK PR first. Pin agents to an immutable commit SHA or
  release tag in `[tool.uv.sources]`; never merge a moving `branch = "main"`
  dependency for this contract. Then merge fwbg server support, then agents.
  Do not activate the new promotion gate between those boundaries.

## Steps

### Step 1: Define the versioned contract and canonical hashing in fwbg-sdk

Create `packages/fwbg-sdk/src/fwbg_sdk/evaluation.py` using only the standard
library. Export:

- `EvaluationRole(StrEnum)` with `DEVELOPMENT_WALK_FORWARD` and
  `FINAL_HOLDOUT`;
- `EvaluationScenario(StrEnum)` with `BASELINE` and `COST_STRESS`;
- `EvaluationEnvelope` and response/artifact `TypedDict`s;
- `canonical_json_bytes(value)` and `sha256_canonical(value)`.

Canonical JSON must sort keys, use UTF-8, compact separators, preserve list
order, and reject NaN/Infinity rather than emitting non-standard JSON. Reject
booleans where an integer/float is expected. Validation rules:

- schema version is integer `1`;
- development has no attempt/source fields and uses baseline unless an
  explicitly development-only stress run is added later;
- final has non-empty lineage/attempt/source IDs, a 64-lowercase-hex artifact
  hash, and one of the two scenarios;
- unknown fields are rejected at the Pydantic API boundary even though the SDK
  type itself is dependency-light.

The validator must reject explicit null for forbidden development fields and
must serialize optional absent fields with `exclude_none=True`. Absence is the
only canonical form.

Add golden vectors in SDK tests. The same semantic object with different dict
insertion order must hash identically; a changed asset, date, cost, role,
attempt, strategy digest, plugin digest, or source hash must change the request
fingerprint.

**Verify**:

```bash
uv run pytest packages/fwbg-sdk/tests/test_evaluation.py -q
```

Expected: golden canonicalization, validation, and mutation cases all pass.

### Step 2: Extend the fwbg run API with typed evaluation metadata and fingerprints

In `src/fwbg/api/runs.py`, add a strict Pydantic evaluation model mirroring the
SDK contract and attach it optionally to `RunStartRequest`. Validate final
requests before creating directories or spawning a process.

Build `request_fingerprint` from the complete resolved semantics, not only the
request body:

- evaluation envelope;
- strategy file SHA-256 and normalized resolved strategy configuration;
- requested and resolved assets/asset classes;
- half-open start/end dates;
- cost multiplier and scenario;
- immutable fwbg engine build/commit digest plus Python and fwbg-sdk versions;
- every referenced plugin FQN and a conservative digest of its complete
  importable distribution/package bundle (manifest, sources, definitions, and
  referenced helper modules), not only the entry file.

Persist `evaluation.json` and `request_fingerprint` atomically in the run
directory before spawn. Pass them to the child process by a bounded manifest
path or explicit CLI arguments, not one large shell string. Add `evaluation`
and `request_fingerprint` to start, list, progress/detail, and terminal result
responses. Legacy requests get `evaluation: null` and still receive a
fingerprint, but are marked ineligible for evidence.

For final runs the idempotency key is `(attempt_id, scenario)`: baseline and
cost stress intentionally share the attempt but are distinct executions. If an
identical tuple is submitted again, return/adopt the existing run only when the
complete fingerprint is identical. Return `409` when the same tuple has
different semantics. Reject a third/unknown scenario.

Before spawn, resolve the source artifact by opaque ID/run/hash and validate its
existence, development role, lineage, boundary, engine/plugin bundle digests,
and request dates. Repeat hash and digest validation in the child immediately
before data preparation to close the API-to-child time-of-check/time-of-use
window. An invalid source must not consume a process slot.

**Verify**:

```bash
uv run pytest tests/test_api_run_start_validation.py tests/test_api_run_spawn.py -q -k 'evaluation or fingerprint or idempot'
```

Expected: invalid combinations fail before spawn; fingerprints cover every
semantic field; exact retry per `(attempt_id, scenario)` is idempotent; changed
retry returns 409; baseline and cost stress can coexist under one attempt.

### Step 3: Produce a complete, immutable candidate manifest from development

Create `src/fwbg/optimization/candidate_manifest.py` and
`src/fwbg/results/evaluation_artifacts.py`. At the end of a successful
development walk-forward run, build canonical candidate content and persist it
in a workspace-backed, content-addressed store such as
`evaluation-artifacts/candidates/<candidate_content_sha256>.json`. Write via a
same-filesystem temporary file, flush/fsync, and create with no-overwrite
semantics; if the hash already exists, bytes must match exactly. A prunable run
directory contains only a pointer/copy, not the sole source of truth.

Separate stable candidate content from volatile provenance. The stable content
hash excludes creation time and run ID. A surrounding manifest records source
run, timestamps, request fingerprint, and the stable content hash and gets its
own artifact hash. The candidate content must include, per symbol:

- schema version, lineage ID, evaluation role, and candidate-format version;
- lineage boundary `B`, development data range, purge/embargo horizon;
- resolved strategy configuration and strategy-file digest;
- selected indicator variant and exact indicator/plugin parameters;
- TP, SL, confidence thresholds, long/short thresholds, RRR, timeout;
- final selected feature names and preprocessing/feature-selection settings;
- model FQN/type and complete frozen hyperparameters;
- all deterministic/random seeds needed to reproduce fitting and predictions;
- exit strategy, exit modifier, and their parameters;
- risk-management FQN plus the exact deployable `risk_per_trade`, circuit
  breaker, risk adjustment, volatility-targeting, and other effective values;
- universe and asset metadata that affect simulation;
- all referenced plugin FQNs with conservative package/distribution bundle
  digests, including imported helpers and definition files;
- immutable engine build/commit digest and fwbg-sdk version.

The outer provenance manifest—not the stable content—contains source run ID,
creation timestamp, development request fingerprint, and artifact-store ID.

Do not build this manifest by re-reading partial public results. Capture values
at the point where `unified_candidate`, selected features, exit settings, and
risk result are all available. Validate it against a strict schema before
writing. Hash the strategy, engine build, SDK, and referenced plugin bundles
before and after each run; a mid-run change makes the run ineligible and no
candidate artifact is emitted.

Return only an opaque artifact descriptor `{schema_version, artifact_id,
candidate_content_sha256, manifest_sha256}` in the terminal API result. Never
accept a client-supplied filesystem path. Pin referenced candidate artifacts
from ordinary run-retention deletion until the lineage is terminal and no
attempt/adoption record references them; GC is explicitly outside this plan.

**Verify**:

```bash
uv run pytest tests/test_evaluation_manifest.py -q
```

Expected: a synthetic development run emits deterministic candidate content
under a stable hash while provenance may differ by run; missing fields,
non-finite values, overwrite, retention deletion, path escape, or source
mutation fail closed.

### Step 4: Implement a physically separate selection-free final evaluator

Create `src/fwbg/optimization/final_evaluation.py` and route only
`role=final_holdout` to it near the CLI/process entry point. Do not call the
ordinary `process_symbol()` pipeline and do not add `if final` branches around
each optimization stage.

For each symbol the final evaluator must:

1. Load the source candidate by opaque ID from the content-addressed artifact
   store and verify both stable-content and provenance hashes.
2. Recompute the current strategy, immutable engine build/SDK, and complete
   referenced plugin bundle digests. Any mismatch rejects the final request and
   requires a fresh development run.
3. Load data using the same pure loader/resampling functions. Training data is
   strictly before `B` after the candidate's purge/embargo. Evaluation data is
   exactly `[B, E)`.
4. Enforce a causal phase contract for data loading, indicators,
   preprocessing, feature selection, models, exits, and risk. Each phase must
   either be stateless and prefix-invariant, or expose an explicit
   `fit(train)` / frozen `transform(train_or_final)` split. Fit preprocessing,
   feature selectors, and ML models only on training data using exactly the
   frozen features/hyperparameters. No training output may change when rows at
   or after `B` are appended. A plugin without a testable causal interface is
   ineligible for final evaluation.
5. Simulate the exact frozen candidate once on final data. Use the frozen risk
   snapshot; do not call `expand_indicator_grid`, `_run_indicator_variants`,
   `merge_unified_settings`, grid search, majority selection, feature
   selection, threshold tuning, exit selection, or risk recalibration.
6. Run baseline and cost-stress as separate scenarios with the same candidate,
   dates, assets, features, model parameters, exits, and risk snapshot. Only
   transaction-cost inputs differ. Cost stress is also `[B, E)`. Use frozen
   seeds and require the prepared-signal/model-prediction digest to match across
   both scenario runs before comparing their simulations.
7. Emit metrics/trades as private scenario results and a hash-linked
   artifact descriptor. Never overwrite the development artifact.

Extract pure preparation or simulation primitives when necessary. Their APIs
must take explicit frozen values; they must not inspect grid configuration or
choose a winner internally.

Add sentinel tests that monkeypatch every forbidden selection function to
raise. A final evaluation must still pass. Add leakage tests with an extreme
future-only pattern: changing final labels/prices may change metrics but must
not change selected fields, model hyperparameters, risk snapshot, or candidate
hash. Add prefix-invariance tests per pipeline phase; changing post-`E` data
must change nothing.

**Verify**:

```bash
uv run pytest tests/test_final_holdout.py -q
```

Expected: all frozen-candidate, date-boundary, forbidden-call, and mutation
sentinels pass.

### Step 5: Add durable evaluation policy, artifacts, and atomic attempts

In fwbg-agents, add these SQLAlchemy models and the next migration after the
live migration head (expected `0012` when executed sequentially):

1. `LineageEvaluationPolicy`: one row per lineage root with frozen boundary,
   half-open final end policy/value, max attempts, creation time, and version.
   `max_attempts` is frozen when this row is atomically created; later config
   changes affect new lineages only.
2. `EvaluationArtifact`: development run ID, strategy/lineage IDs, schema
   version, opaque fwbg artifact ID, stable candidate-content SHA-256,
   provenance-manifest SHA-256, request fingerprint, strategy/engine/SDK/plugin
   digest summary, and timestamp. Source run ID and manifest SHA are unique
   together.
3. `LineageEvaluationAttempt`: stable UUID, lineage root, candidate strategy,
   artifact FK, ordinal, frozen final end `E`, status, claim/start/end times, baseline/cost run IDs
   and fingerprints, per-scenario status (`pending|running|completed|failed`),
   a complete DSR snapshot (global trials, observed runs, across-trial Sharpe
   variance, and snapshot time), private result JSON, and public projection
   JSON. Unique constraints cover `(lineage_root_id, ordinal)` and
   `(lineage_root_id, attempt_id)`.

Make this table the only boundary authority. Refactor
`get_or_freeze_boundary()` to atomically insert/read
`LineageEvaluationPolicy`; `lineage_boundary.json` becomes an optional atomic
public projection regenerated from the DB. Never refreeze from an unreadable
sidecar.

Implement `claim_final_attempt()` in a new
`orchestrator/evaluation_evidence.py`. In one DB transaction, try ordinal 1
through the policy's frozen maximum using SQLite
`INSERT ... ON CONFLICT DO NOTHING RETURNING id` (or a SQLAlchemy savepoint per
ordinal with explicit rollback). The first returned row is the claim. Commit it
before any final data request. Do not catch a uniqueness `IntegrityError` and
continue in an invalid transaction. A claimed/running/failed/crashed attempt
consumes its ordinal permanently. A successful promotion naturally ends
further use.

On startup, mark stale `claimed`/`running` attempts as `crashed` unless an
identical active fwbg request fingerprint for the same scenario can be
reattached. Reattachment does not create another claim. Snapshot every DSR
input available before final data access—global trials, observed runs, and the
across-trial Sharpe variance—at claim time so both scenarios use one statistical
baseline. Add evaluation role to `TrialStat` (or explicitly exclude roles at
write time): final baseline/cost runs must never increase development search
breadth or enter the historical development-Sharpe sample.

Migration tests must upgrade from `0011`, verify constraints, downgrade to
`0011`, and upgrade again without losing pre-existing strategies/trial stats.

**Verify**:

```bash
uv run alembic upgrade head
uv run pytest tests/persistence -q -k 'evaluation or migration or attempt'
```

Expected: migration round trip passes; a concurrent claim test never allocates
the same ordinal and never exceeds the maximum.

### Step 6: Make the Runner and client contract typed and retry-safe

Extend `FwbgClient.start_run()` to require an SDK `EvaluationEnvelope` for all
agent-created runs and return a typed response containing evaluation,
fingerprint, and artifact descriptor. Extend list/detail typing similarly.

Refactor `Runner._acquire_run()` so it computes/receives the expected complete
fingerprint. It may adopt only an active run with exactly that fingerprint and,
for final runs, the same `(attempt ID, scenario)`. A
same-name/different-fingerprint run means the single slot is busy; wait for it.
Never attach to it. A same-attempt-and-scenario but different-fingerprint
response is a hard error; the other scenario is a valid separate run.

Development Runner calls include lineage ID and boundary/end date. After a
successful development run, validate and persist the returned candidate
artifact row before the strategy becomes eligible for promote analysis.

**Verify**:

```bash
uv run pytest tests/tools/test_fwbg_client.py tests/agents/test_runner.py -q
```

Expected: typing/serialization tests pass; exact retry adopts; changes to any
date, asset, cost, role, source, or digest do not adopt.

### Step 7: Replace the promote gate with one claimed, hash-bound final attempt

Refactor `orchestrator/promote_gate.py`:

1. Require a persisted v1 development artifact matching the current strategy
   and lineage. A missing/mismatched artifact returns a public `rerun_development`
   failure without opening final data.
2. Atomically claim one attempt before any fwbg call. Freeze `E` and all DSR
   history inputs in that claim. Use its stable attempt ID for retries.
3. Submit baseline and cost-stress final envelopes using the same source run,
   artifact SHA, boundary `B`, end `E`, universe, and attempt ID. Both scenarios
   use `[B, E)`; cost stress additionally uses the existing multiplier.
4. Persist each scenario's pending/running/completed/failed state and exact
   fingerprint before/after its call. On restart, reattach an identical active
   run or retrieve its terminal result; never rerun an already completed
   scenario. If a completed result cannot be authenticated by its fingerprint,
   terminally fail the consumed attempt rather than starting over.
5. Persist complete metrics/errors/run IDs only in `private_result_json`.
   Persist to `public_projection_json` only overall pass, named check outcomes,
   attempt ordinal/max, generic error categories, and source artifact hash.
   `named check outcomes` is a closed coarse enum only:
   `holdout_criteria`, `cost_stress`, `dsr`, or `execution`. It must not expose
   metric names, comparators, thresholds, values, miss direction, or detailed
   failure strings.
6. Feed DSR from the private baseline trades and the attempt's frozen complete
   DSR snapshot. Keep current thresholds and fail-closed non-finite behavior.
7. Remove the mutable `fail_count` sidecar as runtime authority. Optionally
   write a public compatibility sidecar atomically from the DB projection; it
   must contain no metrics, trade data, run IDs, or detailed errors.
8. `validate_and_apply(Promote)` must fail closed when no fwbg client, v1
   artifact, claimed attempt, or matching passed evidence exists. Delete the
   current "skipping holdout/cost-stress gate" path.

The Analyst prompt and every strategy-improving agent input receive only the
public projection. Mark private artifacts explicitly so generic run-artifact
or prompt-building code cannot load them accidentally. Add a test that embeds
unique canary numbers/text in private metrics/errors and asserts those canaries
do not occur in any Analyst message or public sidecar.

**Verify**:

```bash
uv run pytest tests/orchestrator/test_promote_gate.py tests/orchestrator/test_recommendations.py tests/agents/test_analyst.py -q
```

Expected: gate is fail closed, both scenarios are hash/fingerprint-bound, and
private canaries never reach agent-visible content.

### Step 8: Backfill legacy lineage state without legitimizing old evidence

Create an idempotent dry-run-by-default script, for example
`scripts/backfill_evaluation_evidence.py`:

- derive one policy row from each valid `lineage_boundary.json`;
- inspect every attributable historical `promote_gate` AgentRun, including
  DONE/successful, FAILED, RUNNING/crashed, and sidecar-only evidence. Create a
  conservative consumed placeholder count equal to at least the greater of all
  attributable runs and historical `fail_count`; never count failures only;
- store legacy details privately and label all rows `legacy_unverified`;
- never create a v1 `EvaluationArtifact` from partial legacy fwbg results;
- for an existing BACKTESTED strategy without a v1 artifact, set/report
  `rerun_development_required`; do not fall back to the old holdout.

Add an explicit `Runner.refresh_development_evidence(strategy)` path for this
state. It submits a new `development_walk_forward` run and persists the v1
artifact while leaving the strategy `BACKTESTED`; it does not replay the
illegal `PROPOSED -> BACKTESTED` transition, create a synthetic child, or reuse
old final evidence. On success it invalidates prior candidate-specific promote
eligibility and makes the refreshed artifact the only source for a new claim.
Expose this as an idempotent operator/auto-runner action for
`rerun_development_required` strategies and cover it with a lifecycle test.

The script prints counts only in dry run. `--apply` uses transactions,
idempotency keys, and a backup/checkpoint prerequisite documented in its help.

**Verify**:

```bash
uv run python scripts/backfill_evaluation_evidence.py
uv run python scripts/backfill_evaluation_evidence.py --help
uv run pytest tests/scripts/test_backfill_evaluation_evidence.py -q
```

Expected: dry run writes nothing; repeated apply on a copied fixture DB creates
no duplicates; legacy rows never become valid v1 evidence.

### Step 9: Pin and deploy the shared contract in dependency order

1. Merge/release the fwbg SDK contract and record its immutable commit/tag.
2. Update agents `[tool.uv.sources]` from `branch = "main"` to that immutable
   `rev` or release source; regenerate `uv.lock` with the normal package tool.
3. Merge/deploy the separate fwbg server PR with additive v1 API and legacy
   manual-run support.
4. Run contract tests against the deployed API.
5. Deploy agents migration/code with automatic promotion paused.
6. Run the backfill dry-run, review counts, back up the DB, apply it, then run
   one synthetic development -> final baseline/cost -> public projection flow.
7. Resume automatic promotion only after exact fingerprint adoption and
   private-canary tests pass in the deployed topology.

**Verify**:

```bash
rg -n 'fwbg-sdk = .*branch = "main"' pyproject.toml
uv lock --check
uv run alembic current
```

Expected: the `rg` command has no output, the lock is current, and Alembic is at
the new single head.

## Test plan

- SDK golden canonical JSON/hashes, illegal field combinations, NaN/Infinity.
- API request validation, atomic manifest storage, idempotent attempt retry,
  complete-fingerprint conflict.
- Development manifest completeness for signal and ML strategies, multi-symbol
  candidates, engine/SDK/plugin bundle digests, seeds, exits, and risk
  configuration; content-addressed retention survives run pruning.
- Final evaluator sentinel: forbidden selection/calibration functions patched
  to raise; final path still succeeds.
- Temporal tests: training `<B` minus purge/embargo, final `[B,E)`, post-`E`
  mutation irrelevant, final-label mutation cannot alter candidate choices;
  prefix-invariance/fit-transform contract for every pipeline phase.
- Scenario parity: baseline and cost stress share every field except cost
  inputs/scenario/fingerprint.
- DB concurrency and crash recovery: one ordinal per claimant, crash consumes,
  exact active retry reattaches per scenario, partial baseline completion
  resumes cost stress without rerunning baseline, maximum cannot be exceeded.
- Trial/DSR: all pre-final history inputs freeze at claim and final-role runs do
  not enter the development trial/Sharpe census.
- Leakage canary: no private metric/error/run ID in public sidecar, events, or
  Analyst/Researcher/Translator prompt.
- Migration/backfill: upgrade/downgrade/idempotence and no legacy promotion.
- Legacy BACKTESTED refresh creates development evidence without an illegal
  lifecycle transition or child strategy.
- Cross-repository E2E with synthetic data and no network/broker access.

## Done criteria

- [ ] Every agent-created fwbg run carries evaluation schema v1 and a complete
  canonical request fingerprint.
- [ ] A development run emits a validated immutable candidate manifest with all
  effective per-symbol decisions and complete engine/SDK/plugin dependency
  digests in a retention-safe content-addressed store.
- [ ] Final evaluation uses a separate code path and tests prove it never calls
  selection, merge, tuning, feature-selection, or risk-recalibration functions.
- [ ] Baseline and cost stress apply one candidate to the same `[B,E)` window.
- [ ] Attempt claims are transactional, durable before data access, consume on
  crash/failure, and are concurrency-tested.
- [ ] Run adoption requires exact fingerprint and `(final attempt ID,
  scenario)`.
- [ ] Promotion fails closed without matching passed v1 evidence.
- [ ] Strategy-improving agents receive only the public projection; leakage
  canary tests pass.
- [ ] SDK dependency is pinned immutably; migration/backfill and all repository
  quality gates pass.
- [ ] No production data or private result is committed in a fixture.

## STOP conditions

Stop and report instead of improvising if:

- Plan 014/015 or the Plan 023 leakage subset is not merged/rebased cleanly;
- there are multiple Alembic heads or the next revision is not the expected
  successor—resolve sequencing with the maintainer before generating it;
- the executor cannot enumerate every effective setting needed to replay a
  candidate. Do not ship an incomplete manifest;
- a selected plugin/strategy can change without a stable digest, or a final run
  would need to execute a stale historical source snapshot;
- an immutable engine build/commit or complete transitively executable plugin
  bundle cannot be resolved and hashed;
- a model or preprocessing implementation cannot fit strictly before `B` and
  transform `[B,E)` without selection on the final window;
- any data-loading, indicator, feature, exit, or other pipeline phase is neither
  stateless/prefix-invariant nor expressible as train-only fit plus frozen
  transform;
- the current risk plugin has no way to consume a frozen deployable snapshot;
  define that interface before final evaluation rather than recalibrating;
- any agent, generic artifact loader, event payload, or sidecar still receives
  final metrics or diagnostic errors;
- backfill evidence is insufficient to distinguish historical claims. Consume
  conservative placeholder attempts; never certify them as v1;
- the content-addressed candidate store cannot be protected from ordinary run
  retention for the life of its lineage;
- a verification gate fails twice after a reasonable correction.

## Maintenance notes

- Adding any run-semantic input requires updating the canonical fingerprint
  golden tests. Missing one reopens incorrect adoption.
- Adding a plugin phase requires adding its effective configuration and digest
  to the candidate manifest before it can participate in final evaluation.
- Schema changes are additive new versions. Never reinterpret an existing
  `candidate.v1.json` or evaluation v1 hash.
- Human reviewers may inspect private evidence; automated strategy-generating
  agents must not. Review prompt assembly and generic artifact endpoints on
  every related change.
- Final failure labels can themselves become a weak adaptive signal. Keep the
  public vocabulary coarse and the attempt budget small; changing either is a
  research-governance decision.
