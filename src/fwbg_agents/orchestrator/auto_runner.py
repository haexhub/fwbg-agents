"""Runner auto mode: backtest waiting strategies without a human trigger.

When enabled, a background loop (started from the app lifespan) checks every
`runner_auto_poll_seconds`: if no backtest-ish agent run is active and a
PROPOSED strategy with a strategy.json is waiting, the oldest one is picked
and run — exactly as if the user had clicked "Run Backtest".

Deliberately single-flight: at most one backtest at a time (fwbg runs are
CPU-heavy). Strategies whose auto-triggered backtests already failed
`runner_auto_max_attempts` times are skipped so a broken strategy cannot
starve the queue by being retried forever; a manual run remains possible.

The on/off switch is persisted in the data dir (survives restarts) and
exposed via GET/PUT /agents/runner/auto.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.researcher import ResearcherInput
from fwbg_agents.agents.runner import Runner
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.research_flow import research_and_translate
from fwbg_agents.orchestrator.run_janitor import ORPHAN_ERROR
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)
from fwbg_agents.tools.fwbg_client import FwbgClient
from fwbg_agents.tools.search import BraveClient, FallbackSearchClient, TavilyClient
from fwbg_agents.tools.secrets import get_secret

log = logging.getLogger(__name__)

# Agent runs that mean "a backtest is running or imminent" — research_flow
# and reiterate both end in an automatic backtest of their new strategy.
_BUSY_AGENTS: tuple[str, ...] = ("runner", "research_flow", "reiterate")
_background_tasks: set[asyncio.Task] = set()


def _config_file():
    return settings.data_dir / "runner_auto.json"


def is_enabled() -> bool:
    try:
        return bool(json.loads(_config_file().read_text()).get("enabled", False))
    except (OSError, json.JSONDecodeError):
        return False


def set_enabled(enabled: bool) -> None:
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": bool(enabled)}))
    log.info("runner auto mode %s", "enabled" if enabled else "disabled")


async def pick_next_strategy_id(session: AsyncSession) -> int | None:
    """Oldest PROPOSED strategy that is ready and worth auto-running.

    Returns None when a backtest is already active (single-flight) or no
    candidate qualifies.
    """
    busy = (
        await session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.agent_name.in_(_BUSY_AGENTS),
                AgentRun.status.in_(
                    [AgentRunStatus.RUNNING.value, AgentRunStatus.PENDING.value]
                ),
            )
        )
    ).scalar_one()
    if busy:
        return None

    proposed = (
        await session.execute(
            select(Strategy)
            .where(Strategy.current_state == StrategyState.PROPOSED.value)
            .order_by(Strategy.created_at)
        )
    ).scalars().all()

    for s in proposed:
        if not (strategy_dir(s.slug) / "iteration_001" / "strategy.json").is_file():
            continue  # not translated yet
        failed_attempts = (
            await session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.agent_name == "runner",
                    AgentRun.strategy_id == s.id,
                    AgentRun.status == AgentRunStatus.FAILED.value,
                    # A run failed by the startup janitor was killed by a
                    # restart, not by the strategy — it must not eat an
                    # attempt.
                    or_(AgentRun.error.is_(None), AgentRun.error != ORPHAN_ERROR),
                )
            )
        ).scalar_one()
        if failed_attempts >= settings.runner_auto_max_attempts:
            continue
        return s.id
    return None


async def tick() -> int | None:
    """One auto-mode cycle: pick and run at most one strategy.

    Returns the strategy id that was backtested, or None if nothing ran. The
    backtest is awaited here — the surrounding loop only polls again after
    the run finished, which keeps the single-flight guarantee even if the
    busy-check were to race.
    """
    if not is_enabled():
        return None

    async with SessionLocal() as session:
        sid = await pick_next_strategy_id(session)
    if sid is None:
        return None

    log.info("runner auto mode: starting backtest for strategy %s", sid)
    async with SessionLocal() as session:
        s = (
            await session.execute(select(Strategy).where(Strategy.id == sid))
        ).scalar_one()
        client = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            await Runner(client, session).run(s)
        except Exception:
            # Runner marked its AgentRun failed; the attempt counter keeps a
            # persistently broken strategy from being retried forever.
            log.exception("runner auto mode: backtest failed for strategy %s", sid)
        finally:
            await client.aclose()
    return sid


async def run_loop() -> None:
    """Poll forever; meant to run as an asyncio background task."""
    log.info(
        "runner auto-mode loop started (poll=%ss, enabled=%s)",
        settings.runner_auto_poll_seconds,
        is_enabled(),
    )
    while True:
        try:
            await tick()
        except Exception:
            log.exception("runner auto mode: tick failed")
        await asyncio.sleep(settings.runner_auto_poll_seconds)


async def _count_proposed(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Strategy)
            .where(Strategy.current_state == StrategyState.PROPOSED.value)
        )
    ).scalar_one()


async def _research_is_busy(session: AsyncSession) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.agent_name == "research_flow",
                AgentRun.status.in_(
                    [AgentRunStatus.RUNNING.value, AgentRunStatus.PENDING.value]
                ),
            )
        )
    ).scalar_one()
    return bool(count)


async def _fill_pipeline_background(agent_run_id: int) -> None:
    """Run one asset-agnostic research cycle for pipeline auto-fill.

    Intentionally skips the auto-backtest step — the auto_runner picks up the
    resulting PROPOSED strategy on its next tick.
    """
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()

        tavily = TavilyClient(api_key=get_secret("tavily"))
        brave = BraveClient(api_key=get_secret("brave"))
        search_client = FallbackSearchClient([tavily, brave])
        fwbg = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            strategy_id = await research_and_translate(
                session,
                ResearcherInput(),
                search_client=search_client,
                fanout_n=settings.researcher_fanout_n,
                fwbg_client=fwbg,
            )
            ar.status = AgentRunStatus.DONE.value
            ar.strategy_id = strategy_id
            ar.ended_at = datetime.now(UTC)
            await session.commit()
            log.info(
                "pipeline fill: research done, strategy %s now proposed", strategy_id
            )
        except Exception as exc:
            log.exception(
                "pipeline fill: research failed (agent_run %s)", agent_run_id
            )
            ar.status = AgentRunStatus.FAILED.value
            ar.error = str(exc)
            ar.ended_at = datetime.now(UTC)
            await session.commit()
        finally:
            await fwbg.aclose()
            await tavily.aclose()
            await brave.aclose()


async def pipeline_fill_loop() -> None:
    """Keep PROPOSED strategies at >= pipeline_min_proposed while runner-auto is on.

    Runs independently of run_loop so backtests (which can take hours) don't
    block pipeline refills. Fires at most one research run at a time.
    """
    log.info(
        "pipeline fill loop started (min=%d, poll=%ss)",
        settings.pipeline_min_proposed,
        settings.pipeline_fill_poll_seconds,
    )
    while True:
        await asyncio.sleep(settings.pipeline_fill_poll_seconds)
        if not is_enabled():
            continue
        try:
            async with SessionLocal() as session:
                if await _research_is_busy(session):
                    continue
                count = await _count_proposed(session)
                if count >= settings.pipeline_min_proposed:
                    continue
                log.info(
                    "pipeline fill: %d/%d proposed — triggering research",
                    count,
                    settings.pipeline_min_proposed,
                )
                now = datetime.now(UTC)
                ar = AgentRun(
                    agent_name="research_flow",
                    status=AgentRunStatus.PENDING.value,
                    created_at=now,
                )
                session.add(ar)
                await session.commit()
                await session.refresh(ar)
                task = asyncio.create_task(_fill_pipeline_background(ar.id))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except Exception:
            log.exception("pipeline fill loop: tick failed")
