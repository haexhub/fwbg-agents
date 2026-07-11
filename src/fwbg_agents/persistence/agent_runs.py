"""Lifecycle helpers for AgentRun rows: create, finish, fail.

These three helpers are the single home for the AgentRun envelope (DEBT-01/02):
every agent/flow creates its run via :func:`start_agent_run` and closes it via
:func:`finish_agent_run` (success/terminal) or :func:`fail_agent_run` (classified
error). Each helper also emits the matching lifecycle timeline event
(``agent_run_started`` / ``agent_run_done`` / ``agent_run_failed``) via
:func:`fwbg_agents.run_events.emit_run_event`, so the run's JSONL timeline has
start/end markers and the live SSE dashboard is notified — Plan 006.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.models import AgentRun, AgentRunStatus
from fwbg_agents.run_events import emit_run_event
from fwbg_agents.tools.api_errors import describe_api_error

log = logging.getLogger(__name__)

_TERMINAL = (AgentRunStatus.DONE, AgentRunStatus.FAILED)


async def start_agent_run(
    session: AsyncSession,
    *,
    agent_name: str,
    strategy_id: int | None = None,
    plugin_id: int | None = None,
    input_artifact_path: str | None = None,
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    commit: bool = True,
) -> AgentRun:
    """Create and persist a new AgentRun row, then emit ``agent_run_started``.

    ``status`` defaults to RUNNING; pass PENDING for endpoints that create the
    row synchronously and hand off to a background task, or DONE for a
    synchronous already-complete run (``promote_live``). When ``status`` is
    terminal, ``ended_at`` is stamped to match ``started_at``.

    ``commit=False`` flushes (to obtain the PK) without committing, so the row
    can share a transaction with a following state transition (``promote_live``
    stages its run before ``transition_strategy``); the caller owns the commit.
    """
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name=agent_name,
        status=status.value,
        strategy_id=strategy_id,
        plugin_id=plugin_id,
        input_artifact_path=input_artifact_path,
        started_at=now,
        ended_at=now if status in _TERMINAL else None,
        created_at=now,
    )
    session.add(ar)
    if commit:
        await session.commit()
        await session.refresh(ar)
    else:
        await session.flush()
    emit_run_event(ar.id, "agent_run_started", agent_name=agent_name)
    return ar


async def finish_agent_run(
    session: AsyncSession,
    ar: AgentRun,
    *,
    status: AgentRunStatus,
    output_artifact_path: str | None = None,
    error: str | None = None,
    plugin_id: int | None = None,
    strategy_id: int | None = None,
) -> None:
    """Close an AgentRun with a final status + end time, then emit the terminal event.

    Emits ``agent_run_done`` for DONE, ``agent_run_failed`` otherwise. Optional
    fields (output path, error, plugin_id, strategy_id) are set only when given
    so callers keep whatever was recorded at start.
    """
    run_id = ar.id
    agent_name = ar.agent_name
    ar.status = status.value
    ar.ended_at = datetime.now(UTC)
    if output_artifact_path is not None:
        ar.output_artifact_path = output_artifact_path
    if error is not None:
        ar.error = error
    if plugin_id is not None:
        ar.plugin_id = plugin_id
    if strategy_id is not None:
        ar.strategy_id = strategy_id
    await session.commit()
    event = "agent_run_done" if status == AgentRunStatus.DONE else "agent_run_failed"
    emit_run_event(run_id, event, agent_name=agent_name, error=error)


async def fail_agent_run(
    session: AsyncSession,
    ar: AgentRun,
    exc: BaseException,
    *,
    transient: bool = False,
) -> str:
    """Mark `ar` FAILED with the classified error message and commit.

    Returns the stored message (for event emission at the call site).
    Commit failures are logged, never raised — the error path must not
    mask the original exception. Emits ``agent_run_failed`` on the timeline.
    """
    run_id = ar.id
    agent_name = ar.agent_name
    msg = describe_api_error(exc)
    if transient:
        msg = f"transient: {msg}"
    ar.status = AgentRunStatus.FAILED.value
    ar.ended_at = datetime.now(UTC)
    ar.error = msg
    try:
        await session.commit()
    except Exception:
        log.exception("failed to persist FAILED status (agent_run %s)", ar.id)
    emit_run_event(run_id, "agent_run_failed", agent_name=agent_name, error=msg)
    return msg
