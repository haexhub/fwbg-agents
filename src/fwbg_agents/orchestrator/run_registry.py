"""In-process registry of live agent-run asyncio tasks.

The cancel endpoint flips an AgentRun row to FAILED, but that alone does not
stop the coroutine still doing the work (web searches, LLM calls, an auto
backtest). Flows that run as their own task register it here under the
agent_run_id so cancel can actually abort them; other run shapes (e.g. the
auto-runner's inline analyst pass) simply won't be found and fall back to the
soft DB-only cancel.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}


def register(agent_run_id: int, task: asyncio.Task) -> None:
    """Track `task` under `agent_run_id`; auto-removed when it finishes."""
    _tasks[agent_run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(agent_run_id, None))


def request_cancel(agent_run_id: int) -> bool:
    """Cancel the live task for this run if one is tracked. Returns whether a
    running task was actually signalled."""
    task = _tasks.get(agent_run_id)
    if task is None or task.done():
        return False
    log.info("cancelling live task for agent_run %s", agent_run_id)
    task.cancel()
    return True
