"""In-process registry of live agent-run tool closures, for the MCP bridge.

haex-claude-proxy's spawned `claude` CLI can call a caller's real function
tools mid-task via an authenticated HTTP callback into
`POST /internal/tool-exec/{agent_run_id}` (see api/internal_tools.py). That
endpoint needs to reach the *same* live `@agent.tool_plain` closure
pydantic-ai already built for that run (same DB session, same search
client) — this module is where a run's tools live while it's in flight.

Modeled on `orchestrator/run_registry.py`'s dict-keyed-by-agent_run_id idiom,
plus a per-run lock: Claude/MCP can issue parallel tool calls in one turn,
and the registered closures share one AsyncSession per run, so concurrent
invocations against it must not overlap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager

_tools: dict[int, dict[str, Callable[..., Awaitable[object] | object]]] = {}
_locks: dict[int, asyncio.Lock] = {}


@contextmanager
def registered(agent_run_id: int, tools: dict[str, Callable[..., Awaitable[object] | object]]):
    """Register `tools` under `agent_run_id` for the duration of the `with` block.

    Guarantees teardown on every exit path, including cancellation — the
    registry must never hold a stale closure past its run's lifetime.
    """
    _tools[agent_run_id] = tools
    _locks[agent_run_id] = asyncio.Lock()
    try:
        yield
    finally:
        _tools.pop(agent_run_id, None)
        _locks.pop(agent_run_id, None)


def get(agent_run_id: int, tool_name: str) -> Callable[..., Awaitable[object] | object] | None:
    """Return the registered closure for `tool_name` under `agent_run_id`, if any."""
    return (_tools.get(agent_run_id) or {}).get(tool_name)


def get_lock(agent_run_id: int) -> asyncio.Lock | None:
    """Return the per-run lock serializing concurrent tool calls, if the run is registered."""
    return _locks.get(agent_run_id)
