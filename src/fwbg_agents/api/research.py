"""M4 research API — POST /research/brief, POST /strategies/{id}/reiterate, GET /hypotheses.

Mirrors the M3 runs endpoints: each POST pre-creates an `AgentRun` row so
the caller has an id to poll, then schedules a background task that opens
its own SessionLocal. The Researcher and Translator inside the orchestrator
each track their own AgentRun rows — the row created here is the
"orchestration" envelope, named "research_flow" / "reiterate".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.researcher import ResearcherInput
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.research_flow import (
    reiterate,
    research_and_translate,
)
from fwbg_agents.persistence.database import SessionLocal, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)
from fwbg_agents.tools.search import TavilyClient

log = logging.getLogger(__name__)

router = APIRouter(tags=["research"])


# ---------------------------------------------------------------------------
# Background-task entry points (monkeypatched in tests).
# ---------------------------------------------------------------------------


async def _run_research_background(input: ResearcherInput, agent_run_id: int) -> None:
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        tavily = TavilyClient(api_key=settings.tavily_api_key)
        try:
            strategy_id = await research_and_translate(
                session, input, tavily=tavily
            )
            ar.status = AgentRunStatus.DONE.value
            ar.strategy_id = strategy_id
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(
                strategy_dir(
                    (
                        await session.execute(
                            select(Strategy).where(Strategy.id == strategy_id)
                        )
                    ).scalar_one().slug
                )
                / "iteration_001"
                / "strategy.json"
            )
            await session.commit()
        except Exception as exc:
            log.exception("research background task failed (agent_run %s)", agent_run_id)
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await session.commit()
        finally:
            await tavily.aclose()


async def _run_reiterate_background(parent_id: int, agent_run_id: int) -> None:
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        try:
            child_id = await reiterate(session, parent_id)
            ar.status = AgentRunStatus.DONE.value
            ar.strategy_id = child_id
            ar.ended_at = datetime.now(UTC)
            await session.commit()
        except Exception as exc:
            log.exception("reiterate background task failed (agent_run %s)", agent_run_id)
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await session.commit()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/research/brief", status_code=202)
async def post_research_brief(
    body: ResearcherInput,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off Researcher → Translator. Returns the orchestration AgentRun id."""
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

    background_tasks.add_task(_run_research_background, body, ar.id)
    return {
        "agent_run_id": ar.id,
        "status": "scheduled",
        "message": f"researching {body.asset_class}; poll /agents/runs/{ar.id}",
    }


@router.post("/strategies/{strategy_id}/reiterate", status_code=202)
async def post_strategy_reiterate(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Apply Analyst sidecar to create a child Strategy. 422/409 preconditions."""
    parent = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if parent is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")

    if parent.current_state != StrategyState.BACKTESTED.value:
        raise HTTPException(
            422,
            f"strategy {parent.slug} is in state {parent.current_state!r}; "
            "reiterate requires BACKTESTED",
        )

    sidecar = (
        strategy_dir(parent.slug) / "iteration_001" / "analyst_recommendation.json"
    )
    if not sidecar.is_file():
        raise HTTPException(
            409,
            f"missing analyst_recommendation.json for {parent.slug}; "
            f"run /strategies/{strategy_id}/analyze first",
        )

    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="reiterate",
        status=AgentRunStatus.PENDING.value,
        strategy_id=parent.id,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    background_tasks.add_task(_run_reiterate_background, parent.id, ar.id)
    return {
        "agent_run_id": ar.id,
        "parent_strategy_id": parent.id,
        "status": "scheduled",
        "message": f"re-iterating {parent.slug}; poll /agents/runs/{ar.id}",
    }


@router.get("/hypotheses")
async def list_hypotheses(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List strategies that have a hypothesis_path set, newest first."""
    limit = max(1, min(limit, 200))
    rows = (
        await session.execute(
            select(Strategy)
            .where(Strategy.hypothesis_path.is_not(None))
            .order_by(desc(Strategy.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return {
        "hypotheses": [
            {
                "id": s.id,
                "slug": s.slug,
                "current_state": s.current_state,
                "asset_class": s.asset_class,
                "strategy_family": s.strategy_family,
                "iteration_count": s.iteration_count,
                "parent_strategy_id": s.parent_strategy_id,
                "hypothesis_path": s.hypothesis_path,
                "spec_path": s.spec_path,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in rows
        ]
    }
