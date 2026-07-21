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
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.models import AgentRun, AgentRunStatus
from fwbg_agents.run_events import emit_run_event
from fwbg_agents.tools.api_errors import describe_api_error

log = logging.getLogger(__name__)

_TERMINAL = (AgentRunStatus.DONE, AgentRunStatus.FAILED)

# Flow drill-down (Plan 008 Schritt 5): the id of the flow run currently in
# scope. A flow entry point (research_flow / plugin_author_flow / ...) sets it
# via :func:`use_parent_run`; every child ``start_agent_run`` inside that async
# task then defaults its ``parent_run_id`` to this value. The ContextVar is
# copied per asyncio task, so concurrent flows never cross-link, and children
# are awaited within the same context, so the link propagates without threading
# the id through every agent signature.
_parent_run: ContextVar[int | None] = ContextVar("agent_run_parent_id", default=None)


def current_parent_run() -> int | None:
    """Return the flow run currently scoped via :func:`use_parent_run`.

    ``None`` at top level (an agent running standalone, not under a flow
    envelope). Flow orchestrators read this to emit ``flow_phase`` markers on
    the envelope run without threading its id through every call — Plan
    live-flow-overview WP-B1.
    """
    return _parent_run.get()


def emit_flow_phase(phase: str, **payload: object) -> None:
    """Emit a ``flow_phase`` timeline event on the currently-scoped flow run.

    No-op when not inside a flow (``current_parent_run()`` is ``None``), so a
    flow orchestrator invoked standalone (e.g. the auto-runner's inline analyst
    pass) simply emits nothing. ``phase`` is one of ``researching | critiquing |
    translating | backtesting | analyzing | planning | implementing |
    evaluating``.
    """
    flow_id = _parent_run.get()
    if flow_id is None:
        return
    emit_run_event(flow_id, "flow_phase", phase=phase, **payload)


@contextmanager
def use_parent_run(run_id: int) -> Iterator[None]:
    """Scope ``run_id`` as the parent for AgentRuns created inside the block.

    Set at a flow entry point (typically the first line of the background
    coroutine that drives a flow run); child ``start_agent_run`` calls awaited
    within inherit it. Auto-resets on exit so a following flow in the same task
    is not accidentally re-parented.
    """
    token = _parent_run.set(run_id)
    try:
        yield
    finally:
        _parent_run.reset(token)


async def start_agent_run(
    session: AsyncSession,
    *,
    agent_name: str,
    strategy_id: int | None = None,
    plugin_id: int | None = None,
    parent_run_id: int | None = None,
    input_artifact_path: str | None = None,
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    commit: bool = True,
) -> AgentRun:
    """Create and persist a new AgentRun row, then emit ``agent_run_started``.

    ``status`` defaults to RUNNING; pass PENDING for endpoints that create the
    row synchronously and hand off to a background task, or DONE for a
    synchronous already-complete run (``promote_live``). When ``status`` is
    terminal, ``ended_at`` is stamped to match ``started_at``.

    ``parent_run_id`` links this run to the flow run that spawned it; if not
    passed explicitly it defaults to the flow currently scoped via
    :func:`use_parent_run` (``None`` at top level). Flow-drill-down — Plan 008.

    ``commit=False`` flushes (to obtain the PK) without committing, so the row
    can share a transaction with a following state transition (``promote_live``
    stages its run before ``transition_strategy``); the caller owns the commit.
    """
    now = datetime.now(UTC)
    parent = parent_run_id if parent_run_id is not None else _parent_run.get()
    ar = AgentRun(
        agent_name=agent_name,
        status=status.value,
        strategy_id=strategy_id,
        plugin_id=plugin_id,
        parent_run_id=parent,
        input_artifact_path=input_artifact_path,
        started_at=now,
        ended_at=now if status in _TERMINAL else None,
        created_at=now,
    )
    session.add(ar)
    if commit:
        await session.commit()
        await session.refresh(ar)
        emit_run_event(ar.id, "agent_run_started", agent_name=agent_name)
    else:
        # commit=False (promote_live): the row isn't durable yet and the caller
        # owns the commit. Skip the timeline event — emitting before the commit
        # would leave an orphaned event (and risk SQLite row-id reuse) if the
        # caller's transaction rolls back.
        await session.flush()
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
