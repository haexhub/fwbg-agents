# Plan 013: Make the fwbg backtest concurrency contract explicit

> **Executor instructions**: Follow this plan step by step. Run every verification command and confirm the expected result before moving to the next step. If anything in the "STOP conditions" section occurs, stop and report; do not improvise. When done, update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> - `cd /home/haex/Projekte/fwbg && git diff --stat f76ef8f..HEAD -- src/fwbg/api/runs.py tests/test_api_run_spawn.py README.md docker-compose.yml .env.example`
> - `cd /home/haex/Projekte/fwbg-agents && git diff --stat 75123b0..HEAD -- src/fwbg_agents/config.py src/fwbg_agents/agents/runner.py tests/agents/test_runner.py .env.example`
>
> If any in-scope file changed since this plan was written, compare the "Current state" excerpts against the live code before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: correctness
- **Planned at**: `fwbg` commit `f76ef8f`, `fwbg-agents` commit `75123b0`, 2026-07-15

## Why this matters

`fwbg-agents` assumes the fwbg API has one backtest slot and waits for `429` when busy. The fwbg API default is currently 10 concurrent CLI subprocesses, while docker-compose appears to set `FWBG_MAX_CONCURRENT_RUNS=1`. That mismatch can produce different behavior between local runs and deployed runs: RAM spikes, competing optimizations, and backtests that the agent thought would be serialized.

## Current state

- fwbg default:

```python
# src/fwbg/api/runs.py:33-34
# Limit concurrent CLI subprocesses to prevent resource exhaustion via spam.
MAX_CONCURRENT_RUNS = int(os.environ.get("FWBG_MAX_CONCURRENT_RUNS", "10"))
```

- fwbg-agents expectation:

```python
# src/fwbg_agents/agents/runner.py:441-450
"""Get a job_id for this strategy: adopt an already-active fwbg run
of the same strategy, or start a new one — waiting while fwbg's
single backtest slot is taken.
...
fwbg enforces one concurrent run (FWBG_MAX_CONCURRENT_RUNS=1) and
answers 429 while busy — the intended behaviour then is to wait for
the slot, not to burn a universe attempt.
"""
```

- `tests/test_api_run_spawn.py` already has a single-slot fixture that monkeypatches the value to 1:

```python
# tests/test_api_run_spawn.py:88-95
@pytest.fixture
def _single_slot(monkeypatch):
    import fwbg.api.runs as runs_mod

    monkeypatch.setattr(runs_mod, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(runs_mod, "_active_jobs", {})
    return runs_mod
```

Repo conventions:
- For settings that are runtime contracts between repos, prefer env vars documented in both `.env.example` files and README.
- Keep the 429 behavior; `fwbg-agents` already has retry/wait logic around 429.

## Commands you will need

| Repo | Purpose | Command | Expected on success |
|------|---------|---------|---------------------|
| fwbg | Focused tests | `python -m pytest tests/test_api_run_spawn.py` | exit 0 |
| fwbg | Lint | `ruff check src/ packages/` | exit 0 |
| fwbg-agents | Focused tests | `uv run pytest tests/agents/test_runner.py -k "busy or single or slot"` | exit 0; if no tests selected, run full file |
| fwbg-agents | Lint/typecheck | `uv run ruff check . && uv run mypy src` | exit 0 |

## Scope

**In scope**:
- `/home/haex/Projekte/fwbg/src/fwbg/api/runs.py`
- `/home/haex/Projekte/fwbg/tests/test_api_run_spawn.py`
- `/home/haex/Projekte/fwbg/README.md`
- `/home/haex/Projekte/fwbg/.env.example`
- `/home/haex/Projekte/fwbg/docker-compose.yml`
- `/home/haex/Projekte/fwbg-agents/src/fwbg_agents/config.py`
- `/home/haex/Projekte/fwbg-agents/src/fwbg_agents/agents/runner.py`
- `/home/haex/Projekte/fwbg-agents/.env.example`

**Out of scope**:
- Implementing a real queue or scheduler.
- Per-user/per-strategy concurrency.
- Changing the `/api/runs/start` response shape.
- Changing data-download concurrency.

## Git workflow

- Suggested branches: `advisor/013-run-concurrency-contract` in both repos.
- Commit message examples: `fix(runs): default to one backtest slot`, `docs(config): document fwbg run concurrency`.
- Do not push unless the operator asks.

## Steps

### Step 1: Decide and encode the canonical default

Use one backtest slot as the canonical default, because that is what `fwbg-agents` implements and what the existing docker-compose setting already signals. Change `fwbg/src/fwbg/api/runs.py` to:

```python
MAX_CONCURRENT_RUNS = int(os.environ.get("FWBG_MAX_CONCURRENT_RUNS", "1"))
```

Adjust the comment to say that `1` is the safe default for memory-heavy optimizer runs and for agent orchestration; operators can raise it deliberately.

**Verify**: `cd /home/haex/Projekte/fwbg && python -m pytest tests/test_api_run_spawn.py` exits 0.

### Step 2: Add a default-value regression test

In `tests/test_api_run_spawn.py`, add a small test that reloads or inspects `fwbg.api.runs.MAX_CONCURRENT_RUNS` when the env var is unset. Keep it robust:
- if reloading the module is awkward because of global state, test a small helper such as `_max_concurrent_runs_from_env()` that you introduce in `runs.py`;
- prefer the helper because it is deterministic and easy to test.

Target behavior:
- unset env -> 1;
- `FWBG_MAX_CONCURRENT_RUNS=3` -> 3.

**Verify**: `cd /home/haex/Projekte/fwbg && python -m pytest tests/test_api_run_spawn.py -k concurrent` exits 0.

### Step 3: Document the contract in both repos

In `fwbg/README.md` and `.env.example`, document `FWBG_MAX_CONCURRENT_RUNS`:
- default `1`;
- raise only when the machine has enough RAM/CPU and the caller can tolerate parallel runs;
- `fwbg-agents` expects 429 when the slot is busy and waits.

In `fwbg-agents/.env.example`, add a short note near `FWBG_API_URL` saying the paired fwbg service should run with `FWBG_MAX_CONCURRENT_RUNS=1` unless deliberately tuned.

If docker-compose already sets `FWBG_MAX_CONCURRENT_RUNS=1`, keep it and add a comment if the file style allows comments.

**Verify**:
- `cd /home/haex/Projekte/fwbg && rg -n "FWBG_MAX_CONCURRENT_RUNS" README.md .env.example docker-compose.yml src/fwbg/api/runs.py`
- `cd /home/haex/Projekte/fwbg-agents && rg -n "FWBG_MAX_CONCURRENT_RUNS" .env.example src/fwbg_agents`

### Step 4: Align fwbg-agents wording without changing behavior

In `fwbg-agents/src/fwbg_agents/config.py` and `src/fwbg_agents/agents/runner.py`, keep existing wait-on-429 behavior but update comments/docstrings to describe a configured single-slot contract rather than an implicit universal truth. Do not add new behavior unless tests reveal a mismatch.

**Verify**: `cd /home/haex/Projekte/fwbg-agents && uv run pytest tests/agents/test_runner.py` exits 0.

### Step 5: Run quality gates

Run:
- `cd /home/haex/Projekte/fwbg && ruff check src/ packages/`
- `cd /home/haex/Projekte/fwbg-agents && uv run ruff check . && uv run mypy src`

**Verify**: all commands exit 0.

## Test plan

- Existing `tests/test_api_run_spawn.py` single-slot 429 tests remain green.
- New test proves env default is 1 and override still works.
- Existing `tests/agents/test_runner.py` proves agents still wait/adopt as before.

## Done criteria

- [ ] fwbg default concurrency is 1 unless overridden.
- [ ] Override by `FWBG_MAX_CONCURRENT_RUNS` is tested.
- [ ] Both repos document the same contract.
- [ ] No `/api/runs/start` response shape changes.
- [ ] Focused tests and lint/typecheck gates pass.
- [ ] `plans/README.md` row for 013 is updated.

## STOP conditions

Stop and report if:
- An existing production deployment intentionally depends on default 10 parallel runs.
- The operator wants a queue instead of 429; that is a different, larger plan.
- Tests show `fwbg-agents` cannot safely handle `429` today.

## Maintenance notes

If future work adds a real run queue, this plan's single-slot default can stay as the queue worker concurrency. Do not silently raise the default in code; make it an explicit deployment choice.
