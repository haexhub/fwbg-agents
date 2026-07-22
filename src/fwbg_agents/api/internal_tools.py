"""Internal API for the MCP tool bridge — POST /internal/tool-exec/{agent_run_id}.

haex-claude-proxy's spawned `claude` CLI calls this endpoint (via its own
stdio MCP bridge, `src/mcp-bridge/bridge-server.js` in that repo) to invoke a
live agent run's own pydantic-ai function tool mid-task and get a real
result back — the same closure `@agent.tool_plain` already built for that
run (same DB session, same search client), looked up via tool_registry.

This is new trust-boundary code — no route in this codebase has any auth
today (see fwbg_api_key/X-API-Key, which is client-side only) — so a
missing/mismatched key fails closed (401), and an unset
``internal_tool_exec_key`` disables the whole surface (503) rather than
defaulting open.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from fwbg_agents.config import settings
from fwbg_agents.orchestrator import tool_registry
from fwbg_agents.run_events import emit_run_event

log = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

# Per-event payload cap (chars), matching agents/instrumented.py's llm_tool_call
# / llm_tool_result truncation so the dashboard timeline stays consistent
# regardless of which code path emitted the event.
_TRUNC = 2048


def _truncate(text: str, limit: int = _TRUNC) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} more chars]"


class ToolExecRequest(BaseModel):
    """Body for POST /internal/tool-exec/{agent_run_id}."""

    tool_name: str
    args: dict = Field(default_factory=dict)


async def _invoke(fn, args: dict) -> object:
    """Call a registered tool closure, running a sync one off the event loop.

    Mirrors pydantic-ai's own behavior (it runs sync tools in a worker
    thread) — a sync closure like Analyst's ``query_trades_tool`` must not
    block the event loop when invoked directly here.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(**args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(**args))


@router.post("/internal/tool-exec/{agent_run_id}")
async def post_tool_exec(
    agent_run_id: int,
    body: ToolExecRequest,
    x_internal_tool_key: str | None = Header(default=None),
) -> dict:
    """Invoke a live agent run's registered tool closure and return its result.

    Serialized per-run via tool_registry's lock — the model can issue
    parallel tool calls in one turn, and the registered closures share one
    AsyncSession per run, so concurrent invocations against it must not
    overlap. Never raises for a tool-side failure: those come back as
    ``{"ok": false, "error": ...}`` so the MCP bridge can hand the model a
    normal (if failed) tool result instead of aborting the CLI's turn.
    """
    if settings.internal_tool_exec_key is None:
        raise HTTPException(503, "MCP tool bridge is not configured (internal_tool_exec_key unset)")
    if x_internal_tool_key != settings.internal_tool_exec_key:
        raise HTTPException(401, "invalid or missing X-Internal-Tool-Key")

    fn = tool_registry.get(agent_run_id, body.tool_name)
    lock = tool_registry.get_lock(agent_run_id)
    if fn is None or lock is None:
        raise HTTPException(404, f"no tool '{body.tool_name}' registered for run {agent_run_id}")

    emit_run_event(
        agent_run_id,
        "llm_tool_call",
        round=1,
        tool_name=body.tool_name,
        args=_truncate(json.dumps(body.args, default=str)),
    )
    async with lock:
        try:
            result = await asyncio.wait_for(
                _invoke(fn, body.args), timeout=settings.internal_tool_exec_timeout_seconds
            )
        except Exception as exc:
            error = str(exc)
            emit_run_event(
                agent_run_id,
                "llm_tool_result",
                round=1,
                tool_name=body.tool_name,
                result=_truncate(f"ERROR: {error}"),
            )
            return {"ok": False, "error": error}

    emit_run_event(
        agent_run_id,
        "llm_tool_result",
        round=1,
        tool_name=body.tool_name,
        result=_truncate(json.dumps(result, default=str)),
    )
    return {"ok": True, "result": result}
