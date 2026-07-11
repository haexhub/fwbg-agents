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

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from fwbg_agents.config import settings
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus

log = logging.getLogger(__name__)

ORPHAN_ERROR = "orphaned: the service restarted while this run was active"
TRANSIENT_ERROR = "transient: "
STALE_ERROR = "stale: run exceeded its wall-clock cap and was failed by the janitor"

# Agents whose run legitimately spans a full backtest (hours). They get the
# long runner cap; every other (pure-LLM) agent gets the short cap.
_LONG_CAP_AGENTS = frozenset({"runner", "research_flow"})


async def fail_orphaned_runs() -> int:
    """Fail every PENDING/RUNNING agent run. Returns how many were cleaned."""
    async with SessionLocal() as session:
        stale = (
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.status.in_(
                            [AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
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


def _cap_for(agent_name: str) -> float:
    """Return the wall-clock stale cap in seconds for the given agent name."""
    return (
        settings.runner_poll_timeout_seconds
        if agent_name in _LONG_CAP_AGENTS
        else settings.llm_run_cap_seconds
    )


async def sweep_stale_runs() -> int:
    """Fail RUNNING/PENDING runs older than their per-agent cap.

    Backstop for a run that hangs while the process is still alive (the startup
    janitor only catches restart orphans). Never touches a run younger than its
    cap, so legitimately long backtests are safe.
    """
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        active = (
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.status.in_(
                            [AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        killed: list[int] = []
        for ar in active:
            if ar.started_at is None:
                continue
            started = ar.started_at
            if started.tzinfo is None:  # SQLite round-trips as naive UTC
                started = started.replace(tzinfo=UTC)
            if (now - started).total_seconds() > _cap_for(ar.agent_name):
                ar.status = AgentRunStatus.FAILED.value
                ar.error = STALE_ERROR
                ar.ended_at = now
                killed.append(ar.id)
        if killed:
            await session.commit()
            log.warning("periodic janitor failed %d stale run(s): %s", len(killed), killed)
        return len(killed)


async def prune_run_dirs() -> int:
    """Remove event directories for terminal agent runs older than the retention threshold.

    Only deletes a directory when the matching AgentRun is terminal (DONE/FAILED)
    and its ended_at is older than settings.run_events_retention_days. Directories
    with an unparseable name, no DB row, or a non-terminal/recent run are skipped.
    Returns the number of directories removed.
    """
    if settings.run_events_retention_days <= 0:
        return 0

    runs_root = settings.data_dir / "agent-runs"
    if not runs_root.is_dir():
        return 0

    threshold = datetime.now(UTC) - timedelta(days=settings.run_events_retention_days)
    removed = 0

    async with SessionLocal() as session:
        for entry in runs_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                run_id = int(entry.name)
            except ValueError:
                log.debug("prune_run_dirs: skipping unparseable entry %s", entry.name)
                continue

            ar = (
                await session.execute(select(AgentRun).where(AgentRun.id == run_id))
            ).scalar_one_or_none()

            if ar is None:
                log.debug("prune_run_dirs: skipping %s — no DB row", entry.name)
                continue
            if ar.status not in (AgentRunStatus.DONE.value, AgentRunStatus.FAILED.value):
                continue
            if ar.ended_at is None:
                continue

            ended = ar.ended_at
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=UTC)
            if ended >= threshold:
                continue

            # Offload the blocking removal to a thread so the shared event loop
            # (FastAPI + agent tasks) stays responsive; isolate per-entry so one
            # unremovable directory (permission/lock) doesn't abort the pass.
            try:
                await asyncio.to_thread(shutil.rmtree, entry)
            except OSError as exc:
                log.warning("prune_run_dirs: could not remove %s: %s", entry, exc)
                continue
            removed += 1
            log.info("prune_run_dirs: removed %s (run %d, ended %s)", entry, run_id, ar.ended_at)

    if removed:
        log.info("prune_run_dirs: removed %d stale agent-run director(ies)", removed)
    return removed


async def sweep_loop() -> None:
    """Poll forever; meant to run as an asyncio background task."""
    while True:
        await asyncio.sleep(settings.run_stale_sweep_seconds)
        try:
            await sweep_stale_runs()
        except Exception:
            log.exception("periodic run-janitor sweep failed")
        try:
            await prune_run_dirs()
        except Exception:
            log.exception("periodic run-janitor prune_run_dirs failed")
