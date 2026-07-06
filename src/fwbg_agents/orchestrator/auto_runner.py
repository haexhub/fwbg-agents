"""Runner auto mode: backtest waiting strategies without a human trigger.

When enabled, two independent background loops run concurrently:
- `run_loop`: every `runner_auto_poll_seconds`, if no runner is active, picks
  the oldest PROPOSED strategy with a strategy.json and backtests it.
- `pipeline_fill_loop`: every `pipeline_fill_poll_seconds`, if the PROPOSED
  count is below `pipeline_min_proposed`, triggers a new research cycle.

The two loops are fully independent — research never blocks a backtest and
vice versa. fwbg's own 429 / single-slot enforcement handles any concurrency.

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

import yaml
from sqlalchemy import and_, func, not_, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import (
    Analyst,
    ChangeExit,
    TuneParams,
    _best_symbol_metrics_from_results,
)
from fwbg_agents.agents.researcher import ResearcherInput
from fwbg_agents.agents.runner import Runner
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir, transition_strategy
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.orchestrator.research_flow import reiterate, research_and_translate
from fwbg_agents.orchestrator.run_janitor import ORPHAN_ERROR, TRANSIENT_ERROR
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

_background_tasks: set[asyncio.Task] = set()


def _config_file():
    return settings.data_dir / "runner_auto.json"


def _read_config() -> dict:
    try:
        return json.loads(_config_file().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_config(cfg: dict) -> None:
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))


def is_enabled() -> bool:
    return bool(_read_config().get("enabled", False))


def set_enabled(enabled: bool) -> None:
    cfg = _read_config()
    cfg["enabled"] = bool(enabled)
    _write_config(cfg)
    log.info("runner auto mode %s", "enabled" if enabled else "disabled")


def get_pipeline_min_proposed() -> int:
    v = _read_config().get("pipeline_min_proposed")
    if v is None:
        return settings.pipeline_min_proposed
    return max(0, min(int(v), 20))


def set_pipeline_min_proposed(value: int) -> None:
    value = max(0, min(int(value), 20))
    cfg = _read_config()
    cfg["pipeline_min_proposed"] = value
    _write_config(cfg)
    log.info("pipeline_min_proposed set to %d", value)


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
                AgentRun.agent_name == "runner",
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
            .order_by(nulls_last(Strategy.queue_position), Strategy.created_at)
        )
    ).scalars().all()

    for s in proposed:
        if not (strategy_dir(s.slug) / "iteration_001" / "strategy.json").is_file():
            continue  # not translated yet
        attempts = await _genuine_failed_runner_attempts(session, s.id)
        if attempts >= settings.runner_auto_max_attempts:
            continue
        return s.id
    return None


async def _genuine_failed_runner_attempts(session: AsyncSession, strategy_id: int) -> int:
    """Failed runner attempts that count against the retry cap. Orphaned and
    transient-network failures were not the strategy's fault, so they are
    excluded."""
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.agent_name == "runner",
                AgentRun.strategy_id == strategy_id,
                AgentRun.status == AgentRunStatus.FAILED.value,
                or_(
                    AgentRun.error.is_(None),
                    and_(
                        AgentRun.error != ORPHAN_ERROR,
                        not_(AgentRun.error.like(f"{TRANSIENT_ERROR}%")),
                    ),
                ),
            )
        )
    ).scalar_one()


async def pick_next_backtested_unanalyzed(session: AsyncSession) -> int | None:
    """Oldest BACKTESTED strategy that has no successful Analyst run yet.

    Backfills strategies that were backtested while the Analyst was crashing
    (or before auto-analysis existed): without this they sit in BACKTESTED
    forever, never advancing and clogging the pipeline-fill "active" count.
    """
    rows = (
        await session.execute(
            select(Strategy)
            .where(Strategy.current_state == StrategyState.BACKTESTED.value)
            .order_by(Strategy.created_at)
        )
    ).scalars().all()
    for s in rows:
        done = (
            await session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.agent_name == "analyst",
                    AgentRun.strategy_id == s.id,
                    AgentRun.status == AgentRunStatus.DONE.value,
                )
            )
        ).scalar_one()
        if done == 0 and (
            strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json"
        ).is_file():
            return s.id
    return None


async def abandon_capped_proposed(session: AsyncSession) -> int:
    """Abandon PROPOSED strategies that have exhausted their backtest retry
    budget. They can never be auto-picked again, so leaving them PROPOSED
    clogs the pipeline-fill "active" count and starves fresh research. Returns
    how many were abandoned."""
    proposed = (
        await session.execute(
            select(Strategy).where(
                Strategy.current_state == StrategyState.PROPOSED.value
            )
        )
    ).scalars().all()
    abandoned = 0
    for s in proposed:
        failed = await _genuine_failed_runner_attempts(session, s.id)
        if failed < settings.runner_auto_max_attempts:
            continue
        pm_path = strategy_dir(s.slug) / "post_mortem.yaml"
        pm_path.parent.mkdir(parents=True, exist_ok=True)
        pm_path.write_text(
            yaml.safe_dump(
                {
                    "slug": s.slug,
                    "asset_class": s.asset_class,
                    "strategy_family": s.strategy_family,
                    "summary": (
                        f"Auto-abandoned: {failed} backtest attempts failed "
                        f"(cap={settings.runner_auto_max_attempts}); never reached "
                        "a state the Analyst could evaluate."
                    ),
                    "written_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=False,
            )
        )
        try:
            await transition_strategy(
                session,
                s,
                StrategyState.ABANDONED,
                reason=f"auto: exceeded backtest retry cap ({failed} attempts)",
                payload={"post_mortem_path": str(pm_path)},
                created_by="auto_runner",
            )
            abandoned += 1
            log.info(
                "runner auto mode: abandoned capped strategy %s (%d failed backtests)",
                s.id, failed,
            )
        except Exception:
            log.exception("runner auto mode: could not abandon capped strategy %s", s.id)
    return abandoned


async def _analyze_and_apply(session: AsyncSession, sid: int) -> None:
    """Run the Analyst on a backtested strategy, apply the recommendation, and
    queue an iteration for tune/change-exit. Shared by the fresh-backtest path
    and the backtested-backlog drain."""
    s = (
        await session.execute(select(Strategy).where(Strategy.id == sid))
    ).scalar_one()
    try:
        rec = await Analyst(session).analyze(s)
    except Exception:
        log.exception("runner auto mode: analyst failed for strategy %s", sid)
        return

    results_path = strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json"
    metrics: dict[str, float] = {}
    if results_path.is_file():
        results = json.loads(results_path.read_text())
        metrics = {
            k: float(v)
            for k, v in _best_symbol_metrics_from_results(results).items()
            if isinstance(v, (int, float))
        }

    try:
        await validate_and_apply(session, s, rec, metrics=metrics)
    except Exception as exc:
        log.warning(
            "runner auto mode: analyst recommendation rejected for strategy %s: %s",
            sid, exc,
        )
        return

    # TuneParams / ChangeExit → create a child PROPOSED strategy so the
    # auto-runner picks it up on the next free slot.
    # AddIndicator stays manual — a plugin must be authored first.
    if isinstance(rec, (TuneParams, ChangeExit)):
        try:
            child_id = await reiterate(session, sid)
            log.info(
                "runner auto mode: iteration queued as strategy %s (parent %s)",
                child_id, sid,
            )
        except Exception:
            log.exception("runner auto mode: reiterate failed for strategy %s", sid)


async def tick() -> int | None:
    """One auto-mode cycle. Priority: backtest a waiting PROPOSED strategy;
    if none, drain the backlog of backtested-but-unanalyzed strategies.

    Returns the strategy id that was worked on, or None if nothing ran. The
    heavy work is awaited here — the surrounding loop only polls again after
    it finished, which keeps the single-flight guarantee.
    """
    if not is_enabled():
        return None

    # Housekeeping: capped PROPOSED strategies can never be auto-picked again;
    # abandon them so they stop clogging the pipeline-fill "active" count and
    # fresh research can start.
    async with SessionLocal() as session:
        await abandon_capped_proposed(session)

    async with SessionLocal() as session:
        sid = await pick_next_strategy_id(session)

    if sid is not None:
        log.info("runner auto mode: starting backtest for strategy %s", sid)
        backtest_ok = False
        async with SessionLocal() as session:
            s = (
                await session.execute(select(Strategy).where(Strategy.id == sid))
            ).scalar_one()
            client = FwbgClient(base_url=settings.fwbg_api_url)
            try:
                await Runner(client, session).run(s)
                backtest_ok = True
            except Exception:
                # Runner marked its AgentRun failed; the attempt counter keeps a
                # persistently broken strategy from being retried forever.
                log.exception("runner auto mode: backtest failed for strategy %s", sid)
            finally:
                await client.aclose()

        if backtest_ok:
            async with SessionLocal() as session:
                await _analyze_and_apply(session, sid)
        return sid

    # Nothing to backtest → drain one backtested-but-unanalyzed strategy so
    # the backlog (e.g. from when the Analyst was crashing) advances instead
    # of sitting in BACKTESTED forever.
    async with SessionLocal() as session:
        pending = await pick_next_backtested_unanalyzed(session)
    if pending is not None:
        log.info(
            "runner auto mode: analyzing backtested-but-unanalyzed strategy %s", pending
        )
        async with SessionLocal() as session:
            await _analyze_and_apply(session, pending)
        return pending

    return None


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


async def _active_strategy_count(session: AsyncSession) -> int:
    """Count strategies that are actively being worked on (PROPOSED or BACKTESTED).

    The fill loop uses this instead of a raw PROPOSED count so that a strategy
    currently being backtested or waiting for the analyst does not trigger new
    research prematurely. New research is only started once the entire iteration
    chain for the current strategy has resolved (promoted to paper or abandoned).
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(Strategy)
            .where(
                Strategy.current_state.in_([
                    StrategyState.PROPOSED.value,
                    StrategyState.BACKTESTED.value,
                ])
            )
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
        get_pipeline_min_proposed(),
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
                count = await _active_strategy_count(session)
                min_proposed = get_pipeline_min_proposed()
                if count >= min_proposed:
                    continue
                log.info(
                    "pipeline fill: %d/%d active (proposed+backtested) — triggering research",
                    count,
                    min_proposed,
                )
                now = datetime.now(UTC)
                ar = AgentRun(
                    agent_name="research_flow",
                    status=AgentRunStatus.PENDING.value,
                    started_at=now,
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
