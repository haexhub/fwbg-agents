# Plan 011: Require FWBG API auth and teach fwbg-agents to send the key

> **Executor instructions**: Follow this plan step by step. Run every verification command and confirm the expected result before moving to the next step. If anything in the "STOP conditions" section occurs, stop and report; do not improvise. When done, update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> - `cd /home/haex/Projekte/fwbg && git diff --stat f76ef8f..HEAD -- src/fwbg/api/__init__.py src/fwbg/api/plugins.py tests/test_api.py tests/test_api_plugin_register.py README.md docker-compose.yml .env.example`
> - `cd /home/haex/Projekte/fwbg-agents && git diff --stat 75123b0..HEAD -- src/fwbg_agents/config.py src/fwbg_agents/tools/fwbg_client.py tests/tools/test_fwbg_client.py .env.example docker-compose.yml`
>
> If any in-scope file changed since this plan was written, compare the "Current state" excerpts against the live code before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: security
- **Planned at**: `fwbg` commit `f76ef8f`, `fwbg-agents` commit `75123b0`, 2026-07-15

## Why this matters

The `fwbg` API can start expensive optimization subprocesses, create/delete strategies, upload data, and register executable plugin code. Today it only enforces `X-API-Key` when `FWBG_API_KEY` is set; the documented server command binds to `0.0.0.0`, so a forgotten env var turns the API into an unauthenticated mutating surface. Before making `fwbg` fail closed, `fwbg-agents` must be able to send the same key on every fwbg request.

## Current state

- `/home/haex/Projekte/fwbg/src/fwbg/api/__init__.py` defines optional API-key middleware:

```python
# src/fwbg/api/__init__.py:33-39
class APIKeyMiddleware(BaseHTTPMiddleware):
    """Simple X-API-Key check, enabled via FWBG_API_KEY env var.

    When FWBG_API_KEY is unset the middleware is a no-op so existing
    single-user setups keep working. When set, every request to /api/* must
    carry a matching X-API-Key header.
    """

# src/fwbg/api/__init__.py:87-92
api_key = os.environ.get("FWBG_API_KEY", "").strip()
if api_key:
    app.add_middleware(APIKeyMiddleware, api_key=api_key)
    log.info("API key authentication enabled")
else:
    log.warning("FWBG_API_KEY not set — API is unauthenticated. Set FWBG_API_KEY for production.")
```

- `/home/haex/Projekte/fwbg/src/fwbg/api/plugins.py` accepts executable plugin registration:

```python
# src/fwbg/api/plugins.py:185-193
@router.post("")
def register_plugin(payload: RegisterPluginPayload) -> dict:
    """Register an agent-authored plugin into the fwbg registry.

    Writes verified plugin code to ``~/.fwbg/plugins/agent-authored/<category>/<slug>/``,
    validates it (module import + tests), and refreshes the registry so the plugin
    appears immediately in ``GET /api/plugins``.
```

- `/home/haex/Projekte/fwbg/README.md` documents network binding without mentioning auth:

```bash
# README.md:52-54
uvicorn fwbg.api:app --host 0.0.0.0 --port 8420 --reload
```

- `/home/haex/Projekte/fwbg-agents/src/fwbg_agents/tools/fwbg_client.py` creates an `httpx.AsyncClient` without auth headers:

```python
# src/fwbg_agents/tools/fwbg_client.py:69-73
def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
    self.base_url = base_url
    self._http = http if http is not None else httpx.AsyncClient(base_url=base_url)
    self._owns_http = http is None
```

Repo conventions:
- `fwbg` uses pytest via `python -m pytest`, Ruff via `ruff check src/ packages/`, and conventional commit messages such as `fix(api): ...`.
- `fwbg-agents` uses `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy src`.
- Do not hard-code secret values. Only document env var names.

## Commands you will need

| Repo | Purpose | Command | Expected on success |
|------|---------|---------|---------------------|
| fwbg | Focused tests | `python -m pytest tests/test_api.py tests/test_api_plugin_register.py` | exit 0 |
| fwbg | Lint | `ruff check src/ packages/` | exit 0 |
| fwbg | Full tests if time permits | `python -m pytest` | exit 0 |
| fwbg-agents | Focused tests | `uv run pytest tests/tools/test_fwbg_client.py` | exit 0 |
| fwbg-agents | Lint | `uv run ruff check .` | exit 0 |
| fwbg-agents | Typecheck | `uv run mypy src` | exit 0 |

## Scope

**In scope**:
- `/home/haex/Projekte/fwbg/src/fwbg/api/__init__.py`
- `/home/haex/Projekte/fwbg/tests/test_api.py`
- `/home/haex/Projekte/fwbg/tests/test_api_plugin_register.py`
- `/home/haex/Projekte/fwbg/README.md`
- `/home/haex/Projekte/fwbg/.env.example`
- `/home/haex/Projekte/fwbg/docker-compose.yml`
- `/home/haex/Projekte/fwbg-agents/src/fwbg_agents/config.py`
- `/home/haex/Projekte/fwbg-agents/src/fwbg_agents/tools/fwbg_client.py`
- `/home/haex/Projekte/fwbg-agents/tests/tools/test_fwbg_client.py`
- `/home/haex/Projekte/fwbg-agents/.env.example`
- `/home/haex/Projekte/fwbg-agents/docker-compose.yml`, only if it exists and already configures `FWBG_API_URL`

**Out of scope**:
- OAuth/session auth, user accounts, dashboard login, or browser cookie auth.
- Changing public response shapes.
- Securing `fwbg-agents` own HTTP API; that is a separate deployment decision already listed in `plans/README.md`.
- Rotating any real secret values.

## Git workflow

- Suggested branches: `advisor/011-fwbg-api-auth` in both repos.
- Commit message examples: `feat(api): require api key for mutating server use`, `feat(client): send fwbg api key`.
- Do not push unless the operator asks.

## Steps

### Step 1: Add API-key support to fwbg-agents client

In `fwbg-agents/src/fwbg_agents/config.py`, add `fwbg_api_key: str | None = None` near `fwbg_api_url`. In `fwbg_agents.tools.fwbg_client.FwbgClient`, add an optional `api_key: str | None = None` parameter. If the caller passes no `http`, create `httpx.AsyncClient(base_url=base_url, headers={"X-API-Key": api_key})` when `api_key` is non-empty. If a test supplies `http`, do not mutate that external client; tests can pass headers through a constructed client if needed.

Then update every production construction that currently does `FwbgClient(base_url=settings.fwbg_api_url)` to pass `api_key=settings.fwbg_api_key`. Use `rg -n "FwbgClient\\(base_url=settings\\.fwbg_api_url" src/fwbg_agents` to find call sites.

**Verify**: `cd /home/haex/Projekte/fwbg-agents && rg -n "FwbgClient\\(base_url=settings\\.fwbg_api_url\\)" src/fwbg_agents` returns no matches.

### Step 2: Test header behavior in fwbg-agents

Add tests to `tests/tools/test_fwbg_client.py`:
- default client includes `X-API-Key` when `api_key="secret"` and no custom `http` is passed;
- default client omits the header when `api_key=None`;
- existing tests with injected `http` still pass unchanged.

Use `httpx.MockTransport` like existing tests in that file.

**Verify**: `cd /home/haex/Projekte/fwbg-agents && uv run pytest tests/tools/test_fwbg_client.py` exits 0.

### Step 3: Make fwbg fail closed outside explicit dev mode

In `fwbg/src/fwbg/api/__init__.py`, introduce a boolean env flag such as `FWBG_ALLOW_UNAUTHENTICATED_API`. The intended behavior:
- if `FWBG_API_KEY` is set, keep existing middleware behavior;
- if `FWBG_API_KEY` is empty and `FWBG_ALLOW_UNAUTHENTICATED_API` is truthy (`1`, `true`, `yes`), keep local/dev behavior and log a warning;
- otherwise raise a startup-time `RuntimeError` from `create_app()` with an actionable message.

Do not make docs/openapi paths public when auth is required; current bypass for docs stays acceptable only because non-API paths do not mutate state.

**Verify**: `cd /home/haex/Projekte/fwbg && python - <<'PY'\nimport os\nos.environ.pop('FWBG_API_KEY', None)\nos.environ.pop('FWBG_ALLOW_UNAUTHENTICATED_API', None)\ntry:\n    import fwbg.api\nexcept RuntimeError as exc:\n    assert 'FWBG_API_KEY' in str(exc)\nelse:\n    raise SystemExit('expected RuntimeError')\nprint('ok')\nPY` prints `ok` and exits 0.

### Step 4: Adapt fwbg tests to explicit dev mode and add auth tests

Existing tests call `create_app()` without auth. Update their fixtures to set `FWBG_ALLOW_UNAUTHENTICATED_API=1`, preferably via `monkeypatch`, so old tests remain intentional. Add focused tests covering:
- startup fails when both `FWBG_API_KEY` and `FWBG_ALLOW_UNAUTHENTICATED_API` are absent;
- `/api/plugins` returns `401` without `X-API-Key` when `FWBG_API_KEY` is set;
- `/api/plugins` returns `200` with the matching key.

Good files: `tests/test_api.py` for app-level auth behavior, `tests/test_api_plugin_register.py` for plugin endpoint regression.

**Verify**: `cd /home/haex/Projekte/fwbg && python -m pytest tests/test_api.py tests/test_api_plugin_register.py` exits 0.

### Step 5: Update env/docs for both repos

In `fwbg/.env.example` and `README.md`, document:
- `FWBG_API_KEY` is required unless `FWBG_ALLOW_UNAUTHENTICATED_API=1` is explicitly set for local-only development;
- if binding to `0.0.0.0`, set `FWBG_API_KEY`;
- local quick-start may use loopback plus `FWBG_ALLOW_UNAUTHENTICATED_API=1`.

In `fwbg-agents/.env.example`, document `FWBG_API_KEY` with the same value as the fwbg server. If docker-compose wires both services, pass the env var through to both.

**Verify**:
- `cd /home/haex/Projekte/fwbg && rg -n "FWBG_API_KEY|FWBG_ALLOW_UNAUTHENTICATED_API" README.md .env.example docker-compose.yml`
- `cd /home/haex/Projekte/fwbg-agents && rg -n "FWBG_API_KEY" .env.example src/fwbg_agents/config.py`

### Step 6: Run quality gates

Run:
- `cd /home/haex/Projekte/fwbg-agents && uv run ruff check . && uv run mypy src`
- `cd /home/haex/Projekte/fwbg && ruff check src/ packages/`

If time permits, run full pytest in both repos.

**Verify**: all commands exit 0.

## Test plan

- `fwbg-agents/tests/tools/test_fwbg_client.py`: header injection and non-injection.
- `fwbg/tests/test_api.py`: startup fail-closed and successful authenticated request.
- Existing endpoint tests must intentionally set dev bypass.

## Done criteria

- [ ] `fwbg-agents` can send `X-API-Key` through `FwbgClient`.
- [ ] `fwbg` refuses to start without `FWBG_API_KEY` unless explicit dev bypass is set.
- [ ] Existing tests do not accidentally rely on unauthenticated default behavior.
- [ ] Docs/env examples explain both env vars.
- [ ] Focused tests pass in both repos.
- [ ] `plans/README.md` row for 011 is updated.

## STOP conditions

Stop and report if:
- `fwbg-agents` is already using a different auth mechanism for fwbg.
- Docker or deployment files contain a secret value; do not copy it into commits or plans.
- Making `fwbg` fail closed breaks a production startup path that cannot set env vars.
- Auth requires dashboard/browser session semantics rather than service-to-service API keys.

## Maintenance notes

Once this lands, any new `FwbgClient` construction must pass `settings.fwbg_api_key`. A reviewer should scrutinize tests to ensure they do not globally set unauthenticated mode and mask auth regressions.
