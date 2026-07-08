"""Shared error-path helper for AgentRun rows.

Marks a run FAILED with a classified error message and commits. Centralises
what used to be a copy-pasted 4-line except-block at 16 sites so the error
classification (`describe_api_error`) stays a single convention.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.models import AgentRun, AgentRunStatus
from fwbg_agents.tools.api_errors import describe_api_error

log = logging.getLogger(__name__)


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
    mask the original exception.
    """
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
    return msg
