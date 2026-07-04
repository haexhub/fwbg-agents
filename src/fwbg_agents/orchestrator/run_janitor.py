"""Startup cleanup for orphaned agent runs.

AgentRun rows are marked RUNNING/PENDING while an agent works and flipped to
a terminal state on completion — but only if the process survives. A container
restart (deploy, crash, watchtower update) mid-run leaves the row stuck in a
non-terminal state forever. That is more than cosmetic: the auto-runner's
single-flight check treats any PENDING/RUNNING runner/research_flow/reiterate
run as "a backtest is active" and never starts, so one orphan silently
disables auto mode permanently.

At startup nothing can legitimately be running (single-process service), so
every non-terminal run is by definition an orphan and is failed here. Runs
failed with the orphan marker are excluded from the auto-runner's retry cap —
a restart mid-backtest says nothing about whether the strategy is broken.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus

log = logging.getLogger(__name__)

ORPHAN_ERROR = "orphaned: the service restarted while this run was active"
TRANSIENT_ERROR = "transient: "


async def fail_orphaned_runs() -> int:
    """Fail every PENDING/RUNNING agent run. Returns how many were cleaned."""
    async with SessionLocal() as session:
        stale = (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.status.in_(
                        [AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value]
                    )
                )
            )
        ).scalars().all()
        if not stale:
            return 0
        now = datetime.now(UTC)
        for ar in stale:
            ar.status = AgentRunStatus.FAILED.value
            ar.error = ORPHAN_ERROR
            ar.ended_at = now
        await session.commit()
        log.warning(
            "failed %d orphaned agent run(s) at startup: %s",
            len(stale),
            [ar.id for ar in stale],
        )
        return len(stale)
