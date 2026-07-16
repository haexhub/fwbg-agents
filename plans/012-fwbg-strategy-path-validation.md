# Plan 012: Validate fwbg strategy filenames before filesystem writes

> **Executor instructions**: Follow this plan step by step. Run every verification command and confirm the expected result before moving to the next step. If anything in the "STOP conditions" section occurs, stop and report; do not improvise. When done, update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**: `cd /home/haex/Projekte/fwbg && git diff --stat f76ef8f..HEAD -- src/fwbg/api/strategies.py src/fwbg/api/_paths.py tests/test_api.py`
>
> If any in-scope file changed since this plan was written, compare the "Current state" excerpts against the live code before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: `fwbg` commit `f76ef8f`, 2026-07-15

## Why this matters

The run endpoints already validate path components before reading result files. Strategy CRUD does not use the same boundary: it builds `strategies_dir / f"{name}.json"` from request path parameters and creates filenames by lowercasing/replacing spaces. That makes the safety model inconsistent around files that users and agents can create, update, delete, and commit to git.

## Current state

- Safe path helpers exist in `/home/haex/Projekte/fwbg/src/fwbg/api/_paths.py`:

```python
# src/fwbg/api/_paths.py:22-32
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

def validate_id(value: str, field: str) -> str:
    if not _SAFE_ID_RE.match(value or ""):
        raise HTTPException(400, f"Invalid {field}: {value!r}")
    return value
```

- Strategy creation does not use those helpers:

```python
# src/fwbg/api/strategies.py:119-130
@router.post("")
def create_strategy(body: StrategyCreate) -> dict:
    """Create a new strategy file."""
    strategies_dir = get_strategies_dir()
    filename = body.name.replace(" ", "_").lower()
    filepath = strategies_dir / f"{filename}.json"

    if filepath.exists():
        raise HTTPException(409, f"Strategy already exists: {filename}")

    body.data["name"] = body.name
    filepath.write_text(json.dumps(body.data, indent=2))
```

- Delete similarly trusts `name`:

```python
# src/fwbg/api/strategies.py:222-232
@router.delete("/{name}")
def delete_strategy(name: str) -> dict:
    strategies_dir = get_strategies_dir()
    filepath = strategies_dir / f"{name}.json"
    ...
    filepath.unlink()
```

Repo conventions:
- `fwbg.api._paths.safe_results_path` validates each path component and checks resolved paths stay under the intended root. Reuse this pattern rather than inventing a new regex.
- Tests for strategy CRUD are in `tests/test_api.py` under `TestStrategyEndpoints`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Focused tests | `python -m pytest tests/test_api.py -k StrategyEndpoints` | exit 0 |
| Lint | `ruff check src/ packages/` | exit 0 |
| Full tests if time permits | `python -m pytest` | exit 0 |

## Scope

**In scope**:
- `/home/haex/Projekte/fwbg/src/fwbg/api/strategies.py`
- `/home/haex/Projekte/fwbg/src/fwbg/api/_paths.py`, only if a shared helper is needed
- `/home/haex/Projekte/fwbg/tests/test_api.py`

**Out of scope**:
- Changing strategy JSON schema.
- Removing strategy DELETE.
- Changing git history endpoints beyond applying the same filename validation.
- Changing presets or `_resolve_section`.

## Git workflow

- Suggested branch: `advisor/012-strategy-path-validation`.
- Commit message example: `fix(api): validate strategy file identifiers`.
- Do not push unless the operator asks.

## Steps

### Step 1: Introduce a single strategy filename resolver

In `src/fwbg/api/strategies.py`, import `validate_id` from `fwbg.api._paths`. Add a helper near `SECTION_FIELD_DIRS`:

```python
def _strategy_file(name: str) -> tuple[str, Path]:
    filename = name.replace(" ", "_").lower()
    validate_id(filename, "strategy name")
    strategies_dir = get_strategies_dir().resolve()
    path = (strategies_dir / f"{filename}.json").resolve()
    try:
        path.relative_to(strategies_dir)
    except ValueError:
        raise HTTPException(400, "Path traversal detected")
    return filename, path
```

If you prefer a more general helper in `_paths.py`, keep the behavior equivalent and add tests.

**Verify**: `cd /home/haex/Projekte/fwbg && python -m pytest tests/test_api.py -k "create_and_load_strategy or update_strategy or delete_strategy"` exits 0.

### Step 2: Use the helper in all strategy file endpoints

Apply `_strategy_file()` to:
- `get_strategy`
- `create_strategy`
- `update_strategy`
- `commit_strategy`
- `strategy_history`
- `strategy_version`
- `delete_strategy`

Preserve response filenames and existing behavior for normal names such as `Test Strategy` -> `test_strategy`.

**Verify**: `cd /home/haex/Projekte/fwbg && rg -n "get_strategies_dir\\(\\).*\\n.*filepath|/ f\"\\{name\\}\\.json\"|/ f\"\\{filename\\}\\.json\"" src/fwbg/api/strategies.py` should show no direct endpoint-local path construction outside `_strategy_file`.

### Step 3: Add traversal and invalid-name tests

In `tests/test_api.py`, extend `TestStrategyEndpoints` with tests:
- `POST /api/strategies` with `{"name": "../evil", "data": {}}` returns 400 and does not create files outside `tmp_dir`;
- `PUT /api/strategies/../evil` returns 400;
- `DELETE /api/strategies/../evil` returns 400;
- a valid name with spaces still creates `test_strategy.json`.

Use the existing `strategy_client` fixture so writes go to a temp directory.

**Verify**: `cd /home/haex/Projekte/fwbg && python -m pytest tests/test_api.py -k StrategyEndpoints` exits 0.

### Step 4: Run quality gates

Run `cd /home/haex/Projekte/fwbg && ruff check src/ packages/`. If time permits, run `python -m pytest`.

**Verify**: commands exit 0.

## Test plan

- New regression tests live in `tests/test_api.py`.
- Existing CRUD tests must continue to pass.
- The negative tests must assert both HTTP status and absence of an escaped file.

## Done criteria

- [ ] Every strategy endpoint uses a shared validation helper.
- [ ] Traversal/invalid strategy names return 400.
- [ ] Existing valid CRUD behavior is unchanged.
- [ ] Focused tests and Ruff pass.
- [ ] `plans/README.md` row for 012 is updated.

## STOP conditions

Stop and report if:
- Existing clients intentionally depend on slashes or subdirectories in strategy names.
- Validation needs to allow names outside `_SAFE_ID_RE`; that is a product/API decision.
- The fix requires changing preset path resolution.

## Maintenance notes

If `fwbg` later supports nested strategy folders, do not loosen this helper casually. Introduce an explicit folder model with per-component validation and tests instead.
