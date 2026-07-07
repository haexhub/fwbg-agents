"""M4 research API — POST /research/brief, POST /strategies/{id}/reiterate, GET /hypotheses.

Mirrors the M3 runs endpoints: each POST pre-creates an `AgentRun` row so
the caller has an id to poll, then schedules a background task that opens
its own SessionLocal. The Researcher and Translator inside the orchestrator
each track their own AgentRun rows — the row created here is the
"orchestration" envelope, named "research_flow" / "reiterate".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.researcher import ResearcherInput
from fwbg_agents.agents.runner import Runner
from fwbg_agents.config import settings
from fwbg_agents.orchestrator import run_registry
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
from fwbg_agents.tools.api_errors import describe_api_error
from fwbg_agents.tools.fwbg_client import FwbgClient, FwbgClientError
from fwbg_agents.tools.search import BraveClient, FallbackSearchClient, TavilyClient
from fwbg_agents.tools.secrets import get_secret


def _research_input_path(agent_run_id: int) -> Path:
    p = settings.data_dir / "research_inputs"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{agent_run_id}.json"

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
        tavily = TavilyClient(api_key=get_secret("tavily"))
        brave = BraveClient(api_key=get_secret("brave"))
        search_client = FallbackSearchClient([tavily, brave])
        fwbg = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            strategy_id = await research_and_translate(
                session,
                input,
                search_client=search_client,
                fanout_n=settings.researcher_fanout_n,
                fwbg_client=fwbg,
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

            # Auto-start backtest — runs independently in its own session.
            log.info(
                "research_flow %s: auto-starting backtest for strategy %s",
                agent_run_id,
                strategy_id,
            )
            try:
                async with SessionLocal() as runner_session:
                    s = (
                        await runner_session.execute(
                            select(Strategy).where(Strategy.id == strategy_id)
                        )
                    ).scalar_one()
                    runner = Runner(fwbg, runner_session)
                    await runner.run(s)
                log.info(
                    "research_flow %s: backtest completed for strategy %s",
                    agent_run_id,
                    strategy_id,
                )
            except Exception:
                log.exception(
                    "research_flow %s: auto-backtest failed for strategy %s (non-fatal)",
                    agent_run_id,
                    strategy_id,
                )

        except Exception as exc:
            log.exception("research background task failed (agent_run %s)", agent_run_id)
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = describe_api_error(exc) or str(exc)
            await session.commit()
        finally:
            await fwbg.aclose()
            await tavily.aclose()
            await brave.aclose()


async def _run_reiterate_background(parent_id: int, agent_run_id: int) -> None:
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        fwbg = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            child_id = await reiterate(session, parent_id, fwbg_client=fwbg)
            ar.status = AgentRunStatus.DONE.value
            ar.strategy_id = child_id
            ar.ended_at = datetime.now(UTC)
            await session.commit()
        except Exception as exc:
            log.exception("reiterate background task failed (agent_run %s)", agent_run_id)
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = describe_api_error(exc) or str(exc)
            await session.commit()
        finally:
            await fwbg.aclose()


def _spawn(agent_run_id: int, coro) -> None:
    """Run a background flow as a tracked asyncio task so /cancel can abort it."""
    task = asyncio.create_task(coro)
    run_registry.register(agent_run_id, task)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/research/brief", status_code=202)
async def post_research_brief(
    body: ResearcherInput,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off Researcher → Translator. Returns the orchestration AgentRun id.

    If `asset_class` is provided it is validated against fwbg's asset registry
    (GET /api/assets/classes). Unknown values are rejected with 422 so the
    constrained vocabulary is enforced at intake, not at LLM time.
    """
    if body.asset_class is not None:
        client = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            known_classes = await client.get_asset_classes()
        except FwbgClientError as exc:
            raise HTTPException(502, f"could not reach fwbg asset registry: {exc}") from exc
        finally:
            await client.aclose()
        if body.asset_class not in known_classes:
            raise HTTPException(
                422,
                f"asset_class {body.asset_class!r} is not in fwbg's registry; "
                f"valid values: {sorted(known_classes)}",
            )

    scope = body.asset_class if body.asset_class else "asset-agnostic"
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

    input_path = _research_input_path(ar.id)
    input_path.write_text(body.model_dump_json())
    ar.input_artifact_path = str(input_path)
    await session.commit()

    _spawn(ar.id, _run_research_background(body, ar.id))
    return {
        "agent_run_id": ar.id,
        "status": "scheduled",
        "message": f"researching {scope}; poll /agents/runs/{ar.id}",
    }


@router.post("/strategies/{strategy_id}/reiterate", status_code=202)
async def post_strategy_reiterate(
    strategy_id: int,
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

    _spawn(ar.id, _run_reiterate_background(parent.id, ar.id))
    return {
        "agent_run_id": ar.id,
        "parent_strategy_id": parent.id,
        "status": "scheduled",
        "message": f"re-iterating {parent.slug}; poll /agents/runs/{ar.id}",
    }


@router.post("/agents/runs/{agent_run_id}/cancel", status_code=200)
async def cancel_agent_run(
    agent_run_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cancel a stuck PENDING or RUNNING research run by marking it FAILED."""
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")
    if ar.status not in (AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value):
        raise HTTPException(
            409,
            f"agent_run {agent_run_id} is already in terminal state {ar.status!r}",
        )
    ar.status = AgentRunStatus.FAILED.value
    ar.ended_at = datetime.now(UTC)
    ar.error = "Cancelled by user"
    await session.commit()
    # Abort the live task if this run is a tracked flow (research_flow /
    # reiterate). Inline flows (auto-runner analyst pass) aren't tracked and
    # fall back to this soft DB-only cancel.
    killed = run_registry.request_cancel(agent_run_id)
    return {"id": agent_run_id, "status": ar.status, "task_cancelled": killed}


@router.post("/agents/runs/{agent_run_id}/retry", status_code=202)
async def retry_agent_run(
    agent_run_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Re-schedule a failed or stuck research_flow run with the original input."""
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")
    if ar.agent_name != "research_flow":
        raise HTTPException(422, "retry is only supported for research_flow runs")
    if not ar.input_artifact_path:
        raise HTTPException(
            409,
            f"agent_run {agent_run_id} has no stored input; cannot retry "
            "(run was created before retry support was added)",
        )
    input_path = Path(ar.input_artifact_path)
    if not input_path.exists():
        raise HTTPException(409, f"input file not found at {ar.input_artifact_path}")

    original_input = ResearcherInput.model_validate_json(input_path.read_text())
    now = datetime.now(UTC)
    new_ar = AgentRun(
        agent_name="research_flow",
        status=AgentRunStatus.PENDING.value,
        started_at=now,
        created_at=now,
    )
    session.add(new_ar)
    await session.commit()
    await session.refresh(new_ar)

    new_input_path = _research_input_path(new_ar.id)
    new_input_path.write_text(original_input.model_dump_json())
    new_ar.input_artifact_path = str(new_input_path)
    await session.commit()

    _spawn(new_ar.id, _run_research_background(original_input, new_ar.id))
    return {
        "agent_run_id": new_ar.id,
        "retried_from": agent_run_id,
        "status": "scheduled",
        "message": f"retrying run {agent_run_id} as run {new_ar.id}",
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
                "suggested_universe": s.suggested_universe,
                "model_knowledge_only": s.model_knowledge_only,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in rows
        ]
    }
