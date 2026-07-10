# Plan 003: Turn the advertised quality gates on — mypy clean, scripts linted, CI enforcing

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dc84bd6..HEAD -- .github/workflows/deploy.yml pyproject.toml scripts/`
> On drift, re-run `uv run mypy src` and `uv run ruff check .` and reconcile
> the error inventory below before proceeding.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: 002 (both edit pyproject.toml — land 002 first to avoid conflicts)
- **Category**: dx / tests
- **Planned at**: commit `dc84bd6`, 2026-07-10

## Why this matters

`README.md` (Development section) advertises four gates: `uv run pytest`,
`uv run ruff check .`, `uv run ruff format .`, `uv run mypy src`. Reality at
planning time: CI runs only pytest and a *narrowed* ruff
(`ruff check src/ tests/`), `uv run mypy src` fails with **22 errors in 18
files**, and `uv run ruff check .` fails with **14 errors, all in scripts/**
(hidden by the CI scope). Contributors who run the documented commands hit
red immediately and learn to ignore the signal; type drift accumulates on the
Runner critical path (a `_FwbgClientProto` vs `FwbgClient` protocol mismatch
at `api/runs.py:75` and `orchestrator/auto_runner.py:465`). Additionally
`pytest-cov` is a declared dev dependency that nothing uses — coverage is
never measured.

## Current state

- `.github/workflows/deploy.yml:53-57` — the only quality steps:

  ```yaml
      - name: Lint
        run: uv run ruff check src/ tests/

      - name: Run Tests
        run: uv run pytest
  ```

  (The `test` job checks out a sibling fwbg repo at lines 40–45 and sets
  `working-directory: fwbg-agents` — keep that structure.)

- `uv run mypy src` → "Found 22 errors in 18 files (checked 75 source
  files)". Known clusters: missing `types-PyYAML` stubs
  (`orchestrator/auto_runner.py:28` import of yaml), and a protocol
  signature mismatch — `Runner(...)` receives a concrete `FwbgClient` where
  `_FwbgClientProto` is expected (`api/runs.py:75`,
  `orchestrator/auto_runner.py:465`, `api/research.py:107`); mypy's note
  shows the proto and the class disagree on `start_run` keyword parameters.
- `uv run ruff check .` → 14 errors, all under `scripts/` (m*_smoke.py):
  E741 (ambiguous `l`, `scripts/m3_smoke.py:117`), F401 unused imports
  (`scripts/m2_smoke.py:11`, `scripts/m5c_smoke.py:34`), F541 f-strings
  without placeholders (`scripts/m3_smoke.py:109,116`,
  `scripts/m6b_smoke.py:394`), plus E501/I001.
- `pyproject.toml` dev group declares `pytest-cov>=6.0.0`; no `--cov` flag
  anywhere. `[tool.pytest.ini_options]` currently has only
  `asyncio_mode = "auto"` and `testpaths = ["tests"]`.
- NOT in scope of this plan: `ruff format --check .` currently fails on 87
  files. A bulk reformat is a separate, deliberately isolated commit the
  maintainer should schedule (blame noise) — see Maintenance notes.

## Commands you will need

| Purpose   | Command                     | Expected on success |
|-----------|-----------------------------|---------------------|
| Typecheck | `uv run mypy src`           | exit 0, "no issues" |
| Lint all  | `uv run ruff check .`       | exit 0              |
| Tests     | `uv run pytest -q`          | all pass            |
| Autofix   | `uv run ruff check --fix scripts/` | fixes I001/F401/F541 |

## Scope

**In scope**:
- `pyproject.toml` (dev deps: add `types-PyYAML`; pytest addopts for coverage)
- `src/**` — *type-annotation-level* changes only, to clear the 22 mypy
  errors (protocol signatures, annotations, narrow `type: ignore` with
  reason as last resort)
- `scripts/m*_smoke.py` — the 14 ruff findings
- `.github/workflows/deploy.yml` — widen lint scope, add mypy step, coverage flag

**Out of scope**:
- Any behavioral change. If clearing a mypy error requires changing runtime
  logic (not just annotations/signatures), STOP and report that error.
- Bulk `ruff format` of the repo (see Maintenance notes).
- `tests/**` type errors — mypy scope is `src` only, as documented.

## Git workflow

- Branch: `advisor/003-ci-quality-gates`
- Commit per step, conventional commits (`chore(ci): …`, `fix(types): …`,
  `chore(scripts): …`).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add `types-PyYAML` and clear the stub errors

`uv add --group dev types-PyYAML`, then `uv run mypy src` and confirm the
yaml-stub errors are gone.

**Verify**: `uv run mypy src 2>&1 | grep -c yaml` → 0.

### Step 2: Reconcile `_FwbgClientProto` with `FwbgClient`

Read the mypy notes for `api/runs.py:75`. Locate `_FwbgClientProto` (grep
`_FwbgClientProto` in `src/fwbg_agents/agents/runner.py`). Align the
protocol's method signatures with `FwbgClient`'s actual ones
(`src/fwbg_agents/tools/fwbg_client.py`) — keyword-only params must match
exactly. Do not change `FwbgClient` itself.

**Verify**: `uv run mypy src 2>&1 | grep -c "runs.py\|auto_runner.py:4\|research.py"` → 0 protocol errors remain.

### Step 3: Clear the remaining mypy errors

Work through `uv run mypy src` file by file. Prefer real annotations;
`# type: ignore[code]  # <one-line reason>` only where a fix would change
behavior.

**Verify**: `uv run mypy src` → exit 0. `uv run pytest -q` → all pass.

### Step 4: Fix the 14 scripts/ lint errors

`uv run ruff check --fix scripts/` clears the mechanical ones (F401, F541,
I001); hand-fix E741 (rename `l`) and E501 (wrap lines).

**Verify**: `uv run ruff check .` → exit 0.

### Step 5: Measure coverage

In `pyproject.toml` `[tool.pytest.ini_options]` add:

```toml
addopts = "--cov=fwbg_agents --cov-report=term-missing:skip-covered"
```

Run the suite once and record the baseline percentage in the commit message.
Do NOT add `--cov-fail-under` yet (baseline first, threshold later).

**Verify**: `bash -c 'set -o pipefail; uv run pytest -q 2>&1 | tail -5'` shows a TOTAL coverage line (pipefail ensures test failures propagate through the pipe).

### Step 6: Enforce in CI

In `.github/workflows/deploy.yml`, replace the Lint step and add mypy:

```yaml
      - name: Lint
        run: uv run ruff check .

      - name: Typecheck
        run: uv run mypy src

      - name: Run Tests
        run: uv run pytest
```

(Coverage flags come free via addopts. Keep `working-directory: fwbg-agents`
semantics — these steps inherit the job default.)

**Verify**: `uv run ruff check . && uv run mypy src && uv run pytest -q` all
exit 0 locally — the exact commands CI will run.

## Test plan

No new tests. The gates themselves are the deliverable; the full suite plus
mypy plus ruff passing locally in Step 6 is the acceptance test.

## Done criteria

- [ ] `uv run mypy src` exits 0
- [ ] `uv run ruff check .` exits 0
- [ ] `uv run pytest -q` exits 0 and prints a coverage TOTAL
- [ ] deploy.yml contains lint (`.`), typecheck, and test steps
- [ ] No runtime-behavior diffs (`git diff src/` shows only annotations,
      signatures, imports of typing names)
- [ ] `plans/README.md` status row updated

## STOP conditions

- A mypy error cannot be cleared without changing runtime behavior — report
  the file:line and the behavioral question instead of changing logic.
- The protocol reconciliation in Step 2 reveals the Runner is called with
  arguments `FwbgClient` doesn't accept (that's a live bug, not a type nit) —
  report it.
- More than ~30 mypy errors at execution time (drift) — re-inventory first.

## Maintenance notes

- Bulk `ruff format` (87 files) is deliberately excluded: it should be one
  isolated commit, coordinated with open branches to avoid rebase pain, then
  a `ruff format --check .` CI step. Recommend scheduling it right after this
  plan lands.
- Once a coverage baseline is known, add `--cov-fail-under=<baseline-5>` to
  prevent regression.
- The `types-PyYAML` dev dep must survive future `uv sync` cleanups.
