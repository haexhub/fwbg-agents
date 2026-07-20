# Plan 022: Isolate and constrain agent-authored plugin execution

> **Executor**: Work in disposable branches for `fwbg-agents` and `fwbg`. Run every gate. Stop rather than substituting an ordinary subprocess for a sandbox. The reviewer maintains the index.
>
> **Drift checks**: `git diff --stat 39cf73d..HEAD -- src/fwbg_agents/agents/plugin_evaluator.py src/fwbg_agents/orchestrator/plugin_flow.py`; `git -C ../fwbg diff --stat f76ef8f..HEAD -- src/fwbg/api/plugins.py src/fwbg/pipeline/registry.py`.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: 004, 011
- **Category**: security
- **Planned at**: agents `39cf73d`, fwbg `f76ef8f`, 2026-07-20

## Why this matters

Agent-authored Python is untrusted. Both services currently import it with `exec_module`; generated tests inherit the service user's environment, filesystem, network, and process privileges. Structural validation is not isolation.

## Current state

- `src/fwbg_agents/agents/plugin_evaluator.py:257-302` imports `data/plugins/<slug>/v1/plugin.py` and returns its live `compute` callable.
- `../fwbg/src/fwbg/api/plugins.py:47-55` accepts `slug`, `python_code`, and `tests_code`.
- `../fwbg/src/fwbg/api/plugins.py:203-239` uses the raw slug in a path, imports code in-process, runs unrestricted pytest, then persists it.
- Preserve Plan 004's AST allowlist as defense in depth, never as the sandbox.

## Commands

- Agents: `uv run pytest tests/agents/test_plugin_evaluator.py -q && uv run ruff check src tests && uv run mypy src` → exit 0.
- FWBG: `cd ../fwbg && uv run pytest tests/test_api_plugin_register.py tests/test_no_hardcoded_plugins.py -q && uv run ruff check src packages` → exit 0.

## Scope

In scope: the modules above; a narrow versioned sandbox protocol/runner; runtime/container configuration; focused security tests. Out of scope: indicator mathematics, new plugin kinds, automatic core promotion, or exposing secrets/network to drafts.

## Steps

1. Constrain `RegisterPluginPayload.slug` to a bounded lowercase identifier. Reject separators/dot segments; resolve the target and verify containment below the category root before existence checks or writes. Test absolute paths, traversal, symlinks, and overwrite.
2. Define a versioned JSON protocol for `import`, `contract`, `scenario`, and `tests`. Inputs contain bounded fixture paths/parameters; outputs contain status, declared metadata, capped stdout/stderr, and structured errors—never credentials or arbitrary host paths.
3. Replace `_import_plugin_module` and `_load_compute` with a real isolated worker: non-root, empty allowlisted environment, temporary writable directory, read-only source/SDK mounts, no network, and CPU/RAM/PID/file-size/wall limits. Prefer a container. If unavailable, STOP.
4. Run generated tests in the same boundary. The services receive metadata/results, never a plugin class or callable.
5. Ensure backtests cannot later import the same draft in-process. Either use the worker for runtime computation or require explicit reviewed promotion before discovery.
6. Add regression tests proving denial of sentinel-env reads, writes outside temp, network, excess child processes/time, and slug/symlink escapes. Do not log runnable misuse payloads.

## Done criteria

- [ ] No untrusted module reaches `exec_module` in either service process.
- [ ] No unrestricted pytest subprocess handles generated code.
- [ ] Path and isolation regression tests pass.
- [ ] Focused tests, Ruff, and agents Mypy pass.
- [ ] Only scoped files changed.

## STOP conditions

- No genuine OS/container isolation facility is available.
- Draft plugins require broker credentials or unrestricted network.
- Isolation changes numerical results; report fixture evidence before proceeding.

## Maintenance

Pin the worker image and version the protocol. Cap all inputs/outputs. Keep isolation tests in CI and treat AST checks only as defense in depth.
