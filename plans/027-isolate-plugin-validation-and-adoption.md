# Plan 027: Isolate plugin validation and require hash-bound human adoption

> **Executor instructions**: This is a cross-repository security plan for
> `fwbg`/`fwbg-sdk`, `fwbg-agents`, and `fwbg-dashboard`. Generated Python and
> generated tests are untrusted input. Never import or execute them in the API,
> agents, dashboard, migration, test runner process, or a container holding
> trading/LLM/broker secrets. Follow the order and verification gates exactly.
> If the specified isolation cannot be represented in the production runtime,
> stop and report rather than weakening it. Update the index only after the
> worker, manual adoption, migration, docs, and deployed isolation tests pass.
>
> **Drift check (run first)**:
>
> Run each command from the named repository; do not assume sibling paths:
>
> ```bash
> # fwbg
> git diff --stat f1d75c4..HEAD -- packages/fwbg-sdk src/fwbg tests Dockerfile docker-compose.yml security docs README.md .env.example .github/workflows
> # fwbg-agents
> git diff --stat 3371802..HEAD -- pyproject.toml uv.lock src/fwbg_agents tests scripts docs CLAUDE.md README.md
> # fwbg-dashboard
> git diff --stat 8e26fc2..HEAD -- server pages components types tests package.json bun.lock
> ```
>
> These snapshots predate Plans 025/026. Rebase onto those completed plans and
> compare the semantic evidence in "Current state". A changed plugin lifecycle,
> SDK contract, service-principal API, registry layout, or Alembic head is a STOP
> condition until this plan is reconciled.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plans 004, 011, 022, 025, and 026; execute after 026 to avoid
  SDK and migration-head conflicts
- **Category**: security, architecture, migration, tests, docs
- **Planned at**: plan-index commit `371eb50`, 2026-07-20; audited source
  snapshots: fwbg path-hardening `f1d75c4`, fwbg-agents `3371802`,
  fwbg-dashboard `8e26fc2`

## Why this matters

Agent-authored plugin source is currently imported and executed inside the
agents service, then imported/tested again inside the fwbg API process. Those
processes hold database access, persistent volumes, broker/LLM/service
credentials, and network access. Static checks and deterministic scenarios are
quality gates, not a security boundary; Python code can act during import,
spawn children, read files, open sockets, or modify the plugin registry before
any invariant is checked.

The current happy path also registers a plugin immediately after machine
verification, and the auto-runner can reiterate a strategy while the plugin is
only `VERIFIED`. The shared plugin volume is writable by both API and live bot.
This turns LLM output into code in the trading process without a durable human
decision tied to exact bytes.

This plan moves all untrusted execution into a networkless, secret-free,
resource-constrained worker with one fresh child per job; separates validation from publication;
and requires an authenticated admin to approve the exact reviewed SHA-256.
After adoption, normal backtest/live processes may import that immutable
revision because it is now human-trusted code. Sandboxing every runtime plugin
phase is explicitly not claimed by this plan.

## Security model and fixed decisions

1. **fwbg is the source of truth.** It owns quarantine storage, the filesystem
   queue, validation results, adoption records, immutable runtime revisions,
   and the active revision map. Agents use only the fwbg HTTP API and never
   mount the queue or runtime plugin volume.
2. **Validation does not publish.** A passing worker verdict creates a
   quarantine record and lets the agents plugin become `VERIFIED`; it does not
   refresh the registry or make the plugin usable in a backtest.
3. **Adoption is human and hash-bound.** An authenticated dashboard user with
   `adopt_plugin` approves one displayed SHA-256. fwbg accepts adoption only
   from Plan 025's `dashboard` service principal, records the user subject,
   revalidates the exact bytes, and atomically activates only that SHA. The
   agents service principal is always forbidden from adoption.
4. **Agents wait.** Reiteration and pre-backtest auto-flow require
   `ADOPTED_IN_FWBG`, not `VERIFIED`. `VERIFIED` means awaiting review and does
   not consume the add-indicator request by pretending a backtest occurred.
5. **One fresh unprivileged child per job.** A long-lived trusted Compose
   supervisor claims one job at a time, creates fresh tmpfs scratch, launches a
   new UID-dropped child, writes the verdict, removes scratch, and returns to
   waiting. Do not rely on Docker restart timing for successful short jobs. A
   genuinely new container per job would require a separate orchestrator and is
   out of scope. No Docker socket or container-control API is mounted anywhere
   in the submission path.
6. **Bounded JSON protocol only.** No pickle, arbitrary archive extraction,
   symlinks, absolute paths, or caller-selected output paths.
7. **Normal runtime remains in-process after adoption.** Plugin phases have
   heterogeneous, high-volume typed data; introducing a secure runtime RPC is
   a separate design. If fully autonomous adoption or isolation during every
   trading invocation is required, stop and design that RPC instead.

## Current state

### Untrusted execution

- `fwbg-agents/src/fwbg_agents/agents/plugin_evaluator.py:119-146` loads an
  authored `plugin.py` and invokes its compute callable on scenarios in the
  agents process.
- `plugin_evaluator.py:257-302` uses `importlib` `exec_module()` and constructs a
  discovered class. Import and constructor side effects run before invariants.
- `fwbg/src/fwbg/api/plugins.py:72-97` performs the same unrestricted import in
  the API process.
- `fwbg/src/fwbg/api/plugins.py:225-251` runs submitted pytest code with the API
  interpreter, inherited environment, filesystem, and network. Output is
  captured without a small bound and returned in the HTTP error.
- Plan 022's path-hardening branch validates the slug and contains the target
  directory. That closes a filesystem escape but does not isolate execution.

### Automatic publication and use

- `fwbg-agents/src/fwbg_agents/orchestrator/plugin_flow.py:340-368` transitions
  a successful local evaluation to `VERIFIED` and immediately calls fwbg
  registration.
- `plugin_flow.py:371-438` sends source with `overwrite=True` and treats
  registration as best effort.
- `fwbg/src/fwbg/api/plugins.py:253-283` writes the code directly into the user
  plugin tree and refreshes the live registry after validation.
- `fwbg-agents/src/fwbg_agents/orchestrator/auto_runner.py:345-400` immediately
  reiterates after `VERIFIED`; the pre-backtest path at `:465-513` likewise
  assumes verified means present in the live catalog.
- `fwbg-agents/src/fwbg_agents/api/plugins.py:353-390` accepts manual
  reiteration for `VERIFIED` rather than `ADOPTED_IN_FWBG`.
- The lifecycle already contains the useful distinct transition
  `VERIFIED -> ADOPTED_IN_FWBG` in
  `orchestrator/lifecycle.py:61-66`; the current fast path simply fails to use
  it as the runtime gate.

### Deployment boundary

- `fwbg/docker-compose.yml:15-17,48-50` mounts one writable
  `fwbg-agent-plugins` volume into both API and live bot.
- `docker-compose.yml:99-127` gives the agents container network access and
  numerous broker, LLM, service, and repository inputs. It is not a safe place
  to execute generated code.
- `fwbg/Dockerfile:1-40` runs as root and contains no dedicated validator
  target/user.
- Existing source-of-truth docs
  `docs/plans/2026-07-09-plugin-source-of-truth.md` and
  `2026-07-09-plugin-creation-in-backend.md` explicitly describe the old
  machine-verified fast path. They must be superseded, not left as conflicting
  guidance.

## Validation protocol v1

Define shared strict models in `fwbg-sdk`. The HTTP submit body contains plugin
fields only; fwbg creates `job_id` and `submitted_at` server-side. The internal
queue request contains:

```json
{
  "schema_version": 1,
  "job_id": "uuid",
  "slug": "validated_slug",
  "kind": "plugin kind",
  "version": "declared version",
  "bundle_sha256": "64 lowercase hex",
  "python_code": "text",
  "contract_yaml": "text",
  "spec_md": "text",
  "tests_code": "optional text",
  "scenario_names": ["allowlisted name"],
  "submitted_at": "UTC timestamp"
}
```

A result contains protocol/job/hash/validator versions, `passed|failed|error`,
stable check codes, scenario counts, resource-limit reason, duration, and at
most 16 KiB combined sanitized output. It never contains environment values,
absolute host paths, arbitrary traceback locals, or the submitted source.

`bundle_sha256` is the only adoption hash. Define it as SHA-256 over a domain
separator `FWBG_PLUGIN_BUNDLE_V1` followed by, in fixed order, an 8-byte
big-endian length and the exact UTF-8 bytes of `slug`, `kind`, `version`,
`python_code`, `contract_yaml`, `spec_md`, `tests_code`, and canonical JSON of
`scenario_names`. Do not normalize line endings or trim source. Exclude job ID,
timestamps, submitter, verdict, and validator metadata so an exact retry has
the same bundle hash while every executable/reviewed byte remains bound.

Hard request limits before queueing:

- Python source: 512 KiB;
- generated tests: 256 KiB;
- contract YAML: 128 KiB;
- spec Markdown: 128 KiB;
- scenario count: 1..16 from the SDK allowlist;
- generated fixture/job scratch: 64 MiB;
- ready queue depth: 8 jobs;
- result/log text returned by API: 16 KiB.

Reject the whole request when a UTF-8 byte limit, hash, enum, schema, slug,
scenario, or queue-depth rule fails. Do not truncate executable input.

## Commands you will need

| Repository | Purpose | Command | Expected on success |
|---|---|---|---|
| fwbg SDK | Contract tests | `uv run pytest packages/fwbg-sdk/tests/test_plugin_validation.py -q` | all pass |
| fwbg | Focused API/worker | `uv run pytest tests/test_api_plugin_register.py tests/test_plugin_validation_queue.py tests/test_plugin_adoption.py -q` | all pass |
| fwbg | Full tests | `uv run pytest` | all pass apart from documented skips |
| fwbg | Lint | `uv run ruff check src packages tests` | exit 0 |
| fwbg | Format | `uv run ruff format --check src packages tests` | exit 0 |
| agents | Migration | `uv run alembic upgrade head` | one head, new revision applied |
| agents | Focused tests | `uv run pytest tests/agents/test_plugin_evaluator.py tests/orchestrator/test_plugin_register.py tests/orchestrator/test_auto_runner.py tests/api/test_plugin_reiterate.py tests/persistence -q` | all pass |
| agents | Quality | `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts && uv run mypy src` | exit 0 |
| dashboard | Focused tests | `bun run test:run -- tests/unit/pluginAdoption.test.ts` | new focused file passes |
| dashboard | Quality | `bun run nuxi typecheck && bun run build` | exit 0 |
| deployment | Render | `docker compose config --no-interpolate --quiet` | exit 0 without exposing values |

The isolation acceptance tests run inside the built production topology; unit
tests alone cannot prove mounts, credentials, network, UID, or resource limits.

## Scope

### In scope

`fwbg`:

- shared contract/scenario modules and tests under `packages/fwbg-sdk`
- new `src/fwbg/plugin_validation/**` API-side queue/quarantine/adoption code
- new `src/fwbg/plugin_validator/**` trusted supervisor and unprivileged child
  entry point
- `src/fwbg/api/plugins.py`, `src/fwbg/pipeline/registry.py`, API lifespan wiring
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`
- `.github/workflows/deploy.yml` for the separate validator target build,
  isolation smoke, and immutable image publication
- `security/plugin-validator-seccomp.json` and worker/API tests
- updates to both 2026-07-09 plugin decision documents

`fwbg-agents`:

- SDK immutable pin in `pyproject.toml`/`uv.lock`
- compatibility re-exports from `orchestrator/plugin_contract.py` and
  `scenario_generators.py`, then deletion only after import migration is proven
- `agents/plugin_evaluator.py` rewritten as an HTTP submit/poll adapter; no
  local import/compute
- `tools/fwbg_client.py`, `orchestrator/plugin_flow.py`, `auto_runner.py`,
  lifecycle/API preconditions and tests
- `persistence/models.py`, next Alembic migration, optional reconciliation
  script, docs/CLAUDE.md/README.md

`fwbg-dashboard`:

- Plan 025 permission policy and central backend transport
- plugin detail/review page and components
- admin-only source/diff/adoption proxy routes and tests

### Out of scope

- Automatic or LLM-issued adoption. An agent suggestion is not human approval.
- Git-repository promotion of an adopted plugin. A later PR can promote a proven
  immutable runtime revision into core source.
- Runtime RPC/sandboxing for every call to an adopted plugin. This plan secures
  untrusted validation and the trust transition, not already-approved code.
- General-purpose untrusted Python or arbitrary dependency installation.
  Generated plugins use only the fixed validator image/SDK dependencies.
- Docker socket mounting, privileged containers, host PID/network namespaces,
  Kubernetes, or a distributed queue.
- Silent deletion/GC of quarantine, results, adoption records, or old immutable
  revisions. Retention is a separate auditable operation.

## Git workflow and merge boundaries

- fwbg branch: `advisor/027-isolated-plugin-validator`, based on completed
  Plans 022, 025, and 026.
- agents branch: `advisor/027-hash-bound-plugin-lifecycle`, based on completed
  Plans 025/026.
- dashboard branch: `advisor/027-plugin-adoption-ui`, based on Plan 025.
- Conventional commits such as `feat(plugins): add isolated validation queue`,
  `fix(plugins): require adopted revision`, and
  `feat(plugins): add admin adoption review`.
- Merge/release SDK protocol first, then fwbg API/worker in compatibility mode,
  then agents/dashboard, then disable direct registration. Do not deploy an
  agents build that waits for adoption before the dashboard can perform it.

## Steps

### Step 1: Move the shared contract and deterministic fixtures into fwbg-sdk

After Plan 026's SDK release, add `fwbg_sdk.plugin_validation` containing:

- the strict `PluginContract` types and YAML load/dump helpers currently in
  agents;
- plugin-validation request/result models or dependency-light typed
  serialization plus strict validators;
- the deterministic seeded scenario generators and allowlist;
- canonical SHA-256 over the exact UTF-8 bytes and metadata that comprise a
  bundle.

Adding Pydantic/PyYAML to SDK dependencies is acceptable if the current models
are moved intact; do not maintain subtly different schemas in two repos. Keep
agents modules as deprecated re-exports for one release so old artifacts/imports
do not break. Add alignment tests for plugin-kind enums and golden bundle hashes.

Pin fwbg-agents to the new immutable SDK revision/tag, not the moving `main`
branch. The validator image and API must report the same protocol and SDK
versions or reject the job.

**Verify**:

```bash
uv run pytest packages/fwbg-sdk/tests/test_plugin_validation.py -q
```

Expected: contract, scenario determinism, limits, protocol round-trip, and
golden hashes pass.

### Step 2: Implement a crash-safe filesystem queue and quarantine in fwbg

Create two physically separate configured roots/volumes:

```text
shared spool volume (API + trusted supervisor only):
  ready/       atomic complete request directories
  running/     claimed jobs
  results/     trusted supervisor results
  failed/      malformed/orphaned job metadata
  .admission.lock

API-only state volume (never mounted in validator):
  quarantine/<slug>/<bundle-sha256>/bundle.json
  submissions/<principal>/<idempotency-key>.json
  adoptions/<adoption-id>.json
  active-map.lock
```

The API generates the job UUID. HTTP retry uses a separate bounded
`Idempotency-Key`, scoped to authenticated service principal plus bundle hash;
same principal/key/hash returns the original job, while same key/different hash
returns 409. Submitter ownership and idempotency mappings live only in API
state, not the child-readable job.

API submission validates all limits, recomputes `bundle_sha256`, takes the
spool admission lock, atomically reserves one of eight slots, and writes to a
same-filesystem temporary job directory. Flush files and directory metadata,
then atomically rename into `ready/<job-id>`. Both API and supervisor take the
same lock for state transitions so concurrent submissions cannot exceed depth.
Reject symlinks and anything not represented directly in the JSON protocol.

The worker claims with atomic `ready/<id> -> running/<id>` rename. Results use
temp file + fsync + atomic rename. API result parsing revalidates schema,
job/hash/version, file type, owner/mode where available, and output limits. An
unreadable/mismatched result is an error, never a pass.

Spool/state directories are `root:root 0700`; request/result/state files are
`0600`. Each child scratch directory is owned by UID/GID 65532 and contains
only that copied request's bounded files. The child cannot traverse the spool
or API state. Container tests must assert actual uid/gid/mode values and a
failed child read/write attempt against spool/results.

Store accepted source in API-owned quarantine for review, but never under a
registry-discovered tree. The validator does not mount quarantine. Quarantine
paths are derived from validated slug/hash and checked with `resolve()` /
`is_relative_to()` using Plan 022's containment pattern.

Add recovery rules: stale `running` job becomes failed after a configured
lease; HTTP idempotency behaves as above; a worker/API crash cannot create a
partial ready job or pass verdict. A server-generated job ID is never accepted
from the caller.

**Verify**:

```bash
uv run pytest tests/test_plugin_validation_queue.py -q
```

Expected: atomicity, principal-scoped idempotency, concurrent admission at the
eight-slot boundary, crash recovery, uid/gid/modes, child access denial,
traversal/symlink, limits, queue-full, result-spoof/mismatch, and
concurrent-claim tests all pass.

### Step 3: Build a trusted supervisor with one fresh unprivileged child per job

Add a separate Dockerfile target/entry point and a `validator` dependency extra
that includes pytest only in that target, not the normal runtime target. The
long-lived trusted supervisor validates only outer JSON shape/byte bounds/hash,
copies one job to fresh tmpfs scratch, spawns one child, enforces limits,
validates the child's bounded result, writes the trusted protocol result,
deletes scratch, and waits for the next job. It must never parse contract YAML,
import source, or execute generated tests. Contract YAML is `yaml.safe_load`ed
and strict-schema validated only inside the resource-limited child.

Before `exec`, the child:

- changes to the job scratch directory;
- receives an explicit environment allowlist with locale/encoding/protocol,
  `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`,
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and all common BLAS/OpenMP thread counts
  fixed to `1`; no inherited broker, LLM, DB, service, proxy, home, or
  repository variables;
- drops supplemental groups, then GID/UID to numeric `65532`;
- sets `RLIMIT_CORE=0`, CPU 30 seconds, file size 8 MiB, open files 64, and
  processes 16; the supervisor additionally enforces a 45-second wall timeout
  and kills the whole child process group;
- has no permission to traverse/write the queue/results directory;
- runs contract parsing, import/class validation, mandatory SDK scenarios and
  invariants, then optional generated pytest tests with cache provider and
  third-party plugin autoload disabled.

Drain stdout/stderr continuously into a fixed 16-KiB ring/tail; never use an
unbounded `capture_output`. Convert tracebacks to stable check code plus bounded
sanitized message. Absolute paths become bundle-relative labels.

The root supervisor keeps only the minimum capabilities needed to drop identity
and kill the child. It never executes submitted functions and does not expose
an HTTP server.

**Verify**:

```bash
uv run pytest tests/test_plugin_validator_supervisor.py -q
```

Expected: happy path plus import exit, constructor exit, timeout, fork pressure,
large output, file-size, malformed child result, and cleanup cases pass.

### Step 4: Harden the validator service in Compose and prove the boundary

Add `plugin-validator` to `fwbg/docker-compose.yml` with:

- dedicated validator image target and fixed version/digest;
- `network_mode: none`, no ports, no `depends_on` network dependency;
- only the shared spool volume mounted, no API-state/accounts/data/logs/stats/workspace/runtime
  plugins/quarantine, no source checkout, and never `/var/run/docker.sock`;
- root filesystem read-only; bounded `noexec,nosuid,nodev` tmpfs for `/tmp` and
  `/work`;
- `cap_drop: [ALL]`, `cap_add: [SETUID, SETGID, KILL]`,
  `security_opt: [no-new-privileges:true, seccomp=...]`;
- `pids_limit: 32`, memory about 768 MiB, CPU about 0.5, no swap where supported;
- no environment pass-through except harmless validator protocol/log settings;
- a restart policy for supervisor crashes; ordinary successful jobs create a
  fresh child/scratch lifecycle without exiting the container.

Add `security/plugin-validator-seccomp.json` derived from the runtime's default
profile and deny at least networking/socket creation, ptrace, mount, keyring,
namespace creation, and other syscalls not needed by Python/numpy/pandas. Treat
profile tuning as allow-by-observation on the valid fixture, never by switching
to unconfined.

Create `tests/integration/run_plugin_validator_compose.py` plus a test override.
It uses a unique Compose project, locally builds/tags the validator target,
loads only a committed fake env, creates fresh project-scoped volumes, and
tears down only that exact project/volumes in `finally`. It must not load the
production `.env`, normal named volumes, or the production digest-pinned image.
Production Compose pulls the immutable validator digest; the test override
asserts the local image ID separately.

The acceptance test uses harmless sentinel values in
the other test services' environments/volumes. Submitted fixture code attempts to
observe a sentinel, create a network connection, write outside scratch, exceed
limits, and leave a child behind. The expected result is a failed validation
with stable codes; sentinels never appear in output; host/API/agents/bot data is
unchanged. Inspect the running container to assert mounts, networks, caps, UID,
read-only root, pids, memory, and absence of Docker socket.

Update `.github/workflows/deploy.yml` to build/smoke the separate validator
target with its `validator` extra, run the isolation test on a capable runner,
and publish an immutable tag/digest. Keep the normal runtime target free of
pytest/validator entrypoints.

**Verify**:

```bash
docker compose config --no-interpolate --quiet
uv run python tests/integration/run_plugin_validator_compose.py \
  --fwbg-repo /absolute/path/to/fwbg-worktree
```

Expected: all boundary assertions pass. Unit tests without this container test
are not sufficient to finish the plan.

### Step 5: Replace direct registration with validation-job APIs

Refactor `fwbg/src/fwbg/api/plugins.py` into thin authenticated endpoints over
the queue/quarantine service:

- `POST /api/plugins/validation-jobs`: allowed for Plan 025 dashboard or agents
  service principals; require/generate the idempotency contract above, validate
  and enqueue, return 202/job/bundle hash.
- `GET /api/plugins/validation-jobs/{id}`: return bounded status/result to the
  submitting principal or dashboard admin; enforce ownership from API-only
  submission state rather than data supplied by the caller.
- `GET /api/plugins/quarantine/{slug}/{sha}`: dashboard principal only; return
  source/spec/contract/generated-tests as bounded text for human review.
- keep catalog/source/spec endpoints for adopted runtime plugins.

Remove `_import_plugin_module` and `subprocess.run` from the API. The old direct
`POST /api/plugins` becomes a temporary authenticated 410 response after all
callers migrate, then is removed. It must never retain a compatibility path
that imports/writes source.

A passing result stores validation metadata against the exact quarantine bundle hash
but does not call `reset_registry`, write the runtime volume, or return an FQN
as active.

**Verify**:

```bash
uv run pytest tests/test_api_plugin_register.py tests/test_plugin_validation_queue.py -q
rg -n "exec_module|subprocess\.run|pytest" src/fwbg/api/plugins.py
```

Expected: API tests pass and the `rg` command has no untrusted execution path in
the API module.

### Step 6: Implement durable, revalidated, immutable human adoption

Add `adopt_plugin` to Plan 025's permission enum and admin role. Add an
admin-only dashboard review page that displays slug/kind, validation version,
checks, exact SHA-256, source/spec/contract/tests, and a bounded server-generated
diff against the current active revision. Render escaped code as text, never
HTML, visibly flag bidirectional/invisible control characters, and hash the raw
bytes rather than a display-normalized representation.

The dashboard adoption proxy requires admin session, CSRF, and Origin and sends
the approved hash plus a server-derived actor identifier. Its central transport
must prevent browser override of the actor/service headers.

In fwbg add:

- `POST /api/plugins/adoptions`: requires service principal `dashboard`, takes
  slug/bundle hash/validation job, records actor, and first proves that the
  referenced displayed job already passed for the same hash under a currently
  accepted protocol/SDK/validator-image policy. Failed, timeout, error,
  unknown, revoked-version, or job/hash-mismatched records are not adoptable.
  Only then create durable `pending_revalidation` and enqueue the exact
  quarantined bytes again;
- `GET /api/plugins/adoptions/{id}`: dashboard and agents may read bounded
  status; no source or actor detail to agents;
- a crash-safe reconciler that publishes only after the second job passes with
  the same hash/protocol and then marks the adoption active.

Runtime layout is immutable, for example:

```text
agent-authored/
  revisions/<slug>/<sha256>/{__init__.py,manifest.json,contract.yaml,spec.md,tests.py}
  active.json
```

The API writes a new revision directory atomically and never overwrites it.
All adoption reconciliation takes an exclusive API-state lock and uses an
`active.json` generation/CAS: read generation, write revision, atomically swap a
map with generation+1 only if unchanged, then finalize adoption. The reconciler
is idempotent across crashes before/after revision write, map swap, adoption
status, and registry refresh. Tests cover parallel different slugs, competing
hashes for one slug, lost-update prevention, and every persistence crash point.
Modify registry discovery so only mappings in `active.json` are loaded after
the legacy cutover in Step 8; quarantine and inactive revisions are never
scanned. Store prior mappings for auditable rollback, but rollback itself
requires another admin action.

The API runtime plugin volume remains writable; mount it read-only in `ig-bot`.
Do not hot-restart the live bot as part of adoption. Surface that a controlled
bot restart/redeploy is required before live use; the existing strategy
paper-to-live human gate remains independently mandatory.

Tests must prove: agents principal gets 403 on adoption; admin approves only the
displayed hash; source change/hash mismatch fails; failed revalidation does not
change active map; crash between revision write and map swap leaves the old
active version; registry loads only active; old revision remains immutable. On
successful swap, reset/reload the API registry under the same lock. Existing
backtests keep their already-resolved class; new lookups see the new map. Test
refresh concurrent with a running backtest.

**Verify**:

```bash
# from fwbg
uv run pytest tests/test_plugin_adoption.py -q
# from fwbg-dashboard
bun run test:run -- tests/unit/pluginAdoption.test.ts
```

Expected: the complete permission/hash/atomicity matrix passes.

### Step 7: Make fwbg-agents a submit/poll client with an explicit wait state

Replace local `PluginEvaluator` execution with a client adapter:

1. Read the authored bundle as bytes, enforce client-side limits, calculate the
   SDK `bundle_sha256`, submit it with a stable idempotency key, and poll bounded
   status.
2. In the next Alembic migration after Plan 026 (expected `0013` in sequence),
   add to `VerificationRun`: unique nullable `validation_job_id`,
   `bundle_sha256`, integer `validation_protocol_version`,
   `validator_sdk_version`, immutable `validator_image_digest`, and stable
   `result_code`. Add to `Plugin`: `latest_verified_bundle_sha256`, unique
   nullable `adoption_id`, `adopted_bundle_sha256`, `adopted_at`, and
   `next_adoption_check_at`. Legacy rows are nullable until reconciliation;
   one verification job cannot belong to two runs, and an adopted hash must
   equal a previously verified hash. Never persist secret worker output or
   absolute worker paths.
3. Transition `AUTHORED -> VERIFIED` only for a passing result with the exact
   submitted hash. Delete `_load_compute`, `_evaluate_scenario`, and every local
   import/constructor/compute path after tests move to SDK/fwbg.
4. Delete `_register_verified_plugin_in_fwbg`; there is no overwrite/direct
   registration call.
5. Auto-runner and manual reiteration require
   `PluginState.ADOPTED_IN_FWBG`. A verified plugin produces a clear
   `plugin_awaiting_review` event/state and the strategy remains queued without
   a busy retry loop. Persist `next_adoption_check_at`; use exponential backoff
   from 30 seconds capped at 15 minutes, exclude not-due VERIFIED rows in SQL
   selectors, and emit at most one awaiting-review event per plugin/hash.
6. Poll adoption status or catalog metadata. Transition
   `VERIFIED -> ADOPTED_IN_FWBG` only when fwbg reports active FQN with the same
   bundle SHA. Then and only then resume translation/reiteration.

Add a startup reconciliation pass for pre-existing `VERIFIED` rows: calculate
their bundle hash, submit validation if no matching passed record exists, and
leave them awaiting human review. Never infer adoption merely because a slug is
present; exact active hash is required.

Migration tests cover the actual preceding revision, round-trip, old verified
rows, and idempotent reconciliation. If Plan 026 changed the head, generate the
real next revision rather than reusing `0013`.

**Verify**:

```bash
uv run alembic upgrade head
uv run pytest tests/agents/test_plugin_evaluator.py tests/orchestrator/test_plugin_register.py tests/orchestrator/test_auto_runner.py tests/api/test_plugin_reiterate.py tests/persistence -q
rg -n "exec_module|_load_compute|_evaluate_scenario|register_plugin\(" src/fwbg_agents
```

Expected: all tests pass; `rg` finds no generated-code execution/direct
registration path (SDK/runtime imports unrelated to authored validation must be
reviewed explicitly).

### Step 8: Inventory and human-migrate existing runtime plugins before cutover

Before registry discovery becomes active-map-only, add an idempotent,
dry-run-by-default inventory tool. It enumerates every existing
`agent-authored` runtime bundle, computes the v1 bundle hash from its exact
bytes, records which strategies reference its FQN, copies it into quarantine,
and submits isolated validation. It must not mark anything adopted.

Expose these legacy bundles in the same dashboard review flow. A human must
approve each exact bundle hash that should remain available. For every referenced
strategy, confirm its configured FQN resolves to the newly active exact bundle hash;
unknown, failed, changed, or unreviewed bundles block cutover. Do not
grandfather machine-verified code automatically.

Use two deployment phases: first ship quarantine/validation/adoption while the
old registry remains read-only-compatible and direct registration is disabled;
then require an inventory report with zero referenced-unadopted plugins before
switching discovery to `active.json`. Back up the runtime volume/map before the
switch. A restart after cutover must not make a previously working referenced
plugin disappear silently; fail startup with the missing FQN/hash list.

**Verify**:

```bash
uv run python scripts/migrate_legacy_agent_plugins.py
uv run pytest tests/test_plugin_legacy_cutover.py -q
```

Expected: dry run writes nothing; repeated apply is idempotent; unreviewed
referenced plugins block cutover; no legacy bundle is auto-adopted.

### Step 9: Update the architecture documents so the old fast path is not revived

Update:

- `fwbg-agents/docs/plans/2026-07-09-plugin-source-of-truth.md`;
- `fwbg-agents/docs/plans/2026-07-09-plugin-creation-in-backend.md`;
- relevant `CLAUDE.md`/README safety and lifecycle sections in both repos;
- deployment runbook and environment examples.

Mark both historical documents with a machine-readable `Status: superseded by
Plan 027` header and link the replacement decision. Keep historical prose only
under that marker; active README/CLAUDE/runbook guidance must not describe
"verified -> immediate register/refresh".
Document the new meanings:

- `VERIFIED`: isolated validator passed exact bundle hash, not runtime-visible;
- `ADOPTED_IN_FWBG`: admin approved and fwbg activated that exact bundle hash;
- source of truth: fwbg quarantine/adoption/runtime metadata;
- agents directory: authoring lineage only;
- runtime git promotion: optional later slow path;
- no Docker socket and no secrets/network in validator;
- adopted code is human-trusted and then executes in normal runtime processes.

Include an operator sequence for review/adopt, failed validation, failed
revalidation, restart required for bot, rollback request, and orphan/stale job
recovery. Do not include real tokens or credential values.

**Verify**:

```bash
uv run pytest tests/test_plugin_docs_contract.py -q
```

Expected: superseded docs carry the marker/link; active guidance passes a
targeted lifecycle vocabulary test and describes the new trust transition.

### Step 10: Run a synthetic end-to-end rollout before disabling the old path

In a non-production Compose stack with fake credentials:

1. Author a harmless synthetic indicator through agents.
2. Confirm agents cannot observe sentinel secrets and the worker passes it.
3. Confirm plugin is `VERIFIED`, absent from `/api/plugins`, and the strategy is
   awaiting review.
4. Log in as operator: source view/adoption denied.
5. Log in as admin: review exact bundle hash and adopt.
6. Confirm revalidation, immutable revision, active map, agents transition to
   `ADOPTED_IN_FWBG`, and only then reiteration/backtest.
7. Submit a changed byte under the same slug and prove old approval cannot
   activate it.
8. Exercise timeout/network/write/fork/output fixtures and confirm service
   recovery on the next fresh child lifecycle.
9. Restart API/bot and confirm only the active adopted revision is discovered.

After this passes, turn direct `POST /api/plugins` into 410/remove it and remove
any old write-capable agents/bot volume mount. Keep API write and bot read-only.

**Verify**:

```bash
docker compose config --no-interpolate --quiet
uv run python tests/integration/run_plugin_lifecycle_compose.py \
  --fwbg-repo /absolute/path/to/fwbg-worktree \
  --agents-repo /absolute/path/to/fwbg-agents-worktree \
  --dashboard-repo /absolute/path/to/fwbg-dashboard-worktree
```

Expected: the full trust-transition and isolation suite passes twice from a
clean uniquely named fake stack; no production env/volume is touched and no old
direct-registration path remains.

## Test plan

- Protocol: UTF-8 byte caps, unknown fields/versions, canonical hash, illegal
  slug/kind/scenario, NaN, queue depth, output cap.
- Queue: atomic publish/claim/result, parallel admission at depth eight,
  two-worker race, crash/orphan recovery, ownership/modes, child access denial,
  symlink/path containment, forged/mismatched result, principal-scoped HTTP
  idempotency.
- Supervisor: import/constructor/compute/test failures; CPU/wall/file/pid/output
  limits; child group kill, scratch cleanup, and next fresh child.
- Container boundary: no network, secrets, Docker socket, host/workspace/plugin
  mounts, external writes, persistent child, or writable root.
- API: no local import/pytest, submit/poll permissions, sanitized error, passing
  verdict does not publish.
- Adoption: admin+CSRF+Origin+dashboard principal, actor audit, prior passed
  displayed job/current validator policy, exact bundle hash, mandatory
  revalidation, agents 403, parallel/CAS/crash-safe active map, registry refresh
  during a run, rollback record.
- Agents: no local generated-code execution, lifecycle waits at VERIFIED,
  adopted hash match, migration constraints, startup reconciliation, persisted
  bounded backoff, many ticks without submit/event storms or premature budget
  consumption.
- Dashboard: text-only code rendering, bounded diff, role matrix, stale-hash
  confirmation failure.
- E2E: complete author -> isolated verify -> human adopt -> reiterate flow and
  changed-byte rejection.
- Legacy cutover: inventory/review required, referenced-unadopted blocks, no
  automatic grandfathering or restart disappearance.

## Done criteria

- [ ] Generated plugin source/tests are executed only by the unprivileged child
  inside the networkless, secret-free validator container.
- [ ] API and agents contain no local import/constructor/pytest execution path
  for submitted code.
- [ ] Queue/protocol inputs and outputs are strict, hash-bound, atomic, and
  bounded; separate spool/API-state volumes, ownership, admission,
  idempotency, crash, and concurrency tests pass.
- [ ] A passing validation never writes the runtime plugin tree or refreshes the
  registry.
- [ ] Only an authenticated admin via dashboard service principal can approve
  the displayed hash; fwbg revalidates exact bytes before activation.
- [ ] Runtime revisions are immutable and registry discovery loads only the
  CAS/lock-protected active map; registry refresh is idempotent and bot mount is
  read-only.
- [ ] Agents require `ADOPTED_IN_FWBG` with matching active bundle hash before
  reiteration and handle VERIFIED as a non-busy wait state.
- [ ] Container acceptance tests prove mounts, environment, network, caps,
  resource limits, cleanup, and absence of Docker socket.
- [ ] Old direct registration returns 410/is removed, and old architecture docs
  are marked superseded.
- [ ] Every referenced legacy agent-authored runtime plugin was inventoried and
  explicitly adopted at its exact bundle hash or blocks cutover.
- [ ] All fwbg, agents, dashboard, migration, and Compose E2E gates pass without
  real credentials or production data.

## STOP conditions

Stop and report instead of improvising if:

- Plans 025/026 are incomplete, service principals cannot distinguish dashboard
  from agents, or there is no authenticated admin/CSRF decision;
- organizational policy forbids the minimally privileged root supervisor. Do a
  dedicated rootless `bubblewrap`, gVisor, or equivalent spike; do not run the
  child as root or make the container privileged;
- the production runtime cannot enforce `network_mode: none`, read-only root,
  mounts, capabilities, seccomp, pids, memory, and CPU limits;
- the validator must mount the API-only quarantine/adoption state volume, or
  child UID 65532 can traverse the root-owned spool/results;
- numpy/pandas/plugin SDK cannot run under the seccomp/resource profile. Measure
  the exact required syscall/resource and review a minimal adjustment; never use
  `seccomp=unconfined` or broad mounts as the fix;
- a plugin needs an undeclared dependency or network/data access to validate.
  Extend a reviewed fixed validator image/protocol separately; do not allow
  package installation or arbitrary mounts per job;
- bundle bytes cannot be preserved and rehashed identically through quarantine,
  review, revalidation, and activation;
- more than one fwbg API replica can mutate `active.json` without a shared
  filesystem lock/CAS primitive. Design a transactional store first;
- adoption cannot prove that the exact displayed original job passed under an
  accepted validator image/protocol before revalidation;
- any referenced legacy runtime plugin cannot be inventoried and human-reviewed
  before active-map-only cutover;
- an existing external client depends on direct `POST /api/plugins`; migrate it
  before disabling the endpoint;
- the desired product requirement is fully autonomous runtime adoption or
  isolation during all backtest/live plugin calls. That requires a separately
  reviewed runtime RPC/data-transfer architecture;
- the container isolation acceptance test cannot run in CI/staging, or a
  verification gate fails twice after a reasonable correction.

## Maintenance notes

- The worker image is part of the verdict. Record its immutable digest and SDK
  version; a materially changed validator requires revalidation policy.
- Review queue/quarantine/runtime volume permissions and Compose mounts on every
  deployment change. One accidental secret or workspace mount defeats the
  boundary.
- New plugin kinds/scenarios must be added once in SDK, with protocol and limit
  tests, before agents may request them.
- Do not garbage-collect quarantine/adoption/revisions until an explicit
  retention plan preserves auditability and active references.
- Human review is a real operating cost. If it becomes the throughput
  bottleneck, improve review batching/diffs—not automatic approval.
- An adopted plugin is trusted code. Strategy paper/live promotion remains a
  separate gate and should display the exact plugin revision hashes from Plan
  026's candidate manifest.
