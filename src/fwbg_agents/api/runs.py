"""M3 runs API — POST /strategies/{id}/run, /analyze, GET /agents/runs/{id}.

Both POST endpoints schedule a background task and return 202 immediately;
the background task creates a fresh DB session (the request session is closed
once the response is sent) and runs the agent. The endpoint pre-creates a
PENDING AgentRun row so callers can poll for status; the agent updates it
in-place to RUNNING / DONE / FAILED.

Tests monkeypatch `_run_runner_background` / `_run_analyst_background` to
avoid hitting fwbg or a real LLM.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import Analyst
from fwbg_agents.agents.runner import Runner
from fwbg_agents.config import settings
from fwbg_agents.orchestrator import auto_runner
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.persistence.database import SessionLocal, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
)
from fwbg_agents.tools.fwbg_client import FwbgClient

log = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


def _serialize_agent_run(ar: AgentRun) -> dict[str, Any]:
    return {
        "id": ar.id,
        "agent_name": ar.agent_name,
        "status": ar.status,
        "strategy_id": ar.strategy_id,
        "plugin_id": ar.plugin_id,
        "input_artifact_path": ar.input_artifact_path,
        "output_artifact_path": ar.output_artifact_path,
        "error": ar.error,
        "started_at": ar.started_at.isoformat() if ar.started_at else None,
        "ended_at": ar.ended_at.isoformat() if ar.ended_at else None,
    }


# ---------------------------------------------------------------------------
# Background-task entry points (monkeypatched in tests).
# ---------------------------------------------------------------------------


async def _run_runner_background(strategy_id: int) -> None:
    async with SessionLocal() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        client = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            runner = Runner(client, session)
            await runner.run(s)
        except Exception:
            log.exception("runner background task failed for strategy %s", strategy_id)
        finally:
            await client.aclose()


async def _run_analyst_background(strategy_id: int) -> None:
    async with SessionLocal() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        analyst = Analyst(session)
        try:
            rec = await analyst.analyze(s)
            iteration_dir = strategy_dir(s.slug) / "iteration_001"
            results_path = iteration_dir / "fwbg_results.json"
            metrics: dict[str, float] = {}
            if results_path.is_file():
                import json as _json

                results = _json.loads(results_path.read_text())
                from fwbg_agents.agents.analyst import _best_symbol_metrics_from_results

                metrics = {
                    k: float(v)
                    for k, v in _best_symbol_metrics_from_results(results).items()
                    if isinstance(v, (int, float))
                }
            try:
                await validate_and_apply(session, s, rec, metrics=metrics)
            except Exception as exc:
                log.warning("analyst recommendation rejected: %s", exc)
        except Exception:
            log.exception("analyst background task failed for strategy %s", strategy_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/runner/auto")
async def get_runner_auto() -> dict[str, Any]:
    """Current state of the Runner auto mode (persisted flags)."""
    return {
        "enabled": auto_runner.is_enabled(),
        "pipeline_min_proposed": auto_runner.get_pipeline_min_proposed(),
    }


class RunnerAutoUpdate(BaseModel):
    enabled: bool | None = None
    pipeline_min_proposed: int | None = None


@router.put("/runner/auto")
async def put_runner_auto(body: RunnerAutoUpdate) -> dict[str, Any]:
    """Update Runner auto mode settings. Any omitted field is left unchanged."""
    if body.enabled is not None:
        auto_runner.set_enabled(body.enabled)
    if body.pipeline_min_proposed is not None:
        auto_runner.set_pipeline_min_proposed(body.pipeline_min_proposed)
    return {
        "enabled": auto_runner.is_enabled(),
        "pipeline_min_proposed": auto_runner.get_pipeline_min_proposed(),
    }


@router.post("/strategies/{strategy_id}/run", status_code=202)
async def post_strategy_run(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")

    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.PENDING.value,
        strategy_id=strategy_id,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    background_tasks.add_task(_run_runner_background, strategy_id)
    return {"strategy_id": strategy_id, "agent_run_id": ar.id, "status": "scheduled"}


@router.post("/strategies/{strategy_id}/analyze", status_code=202)
async def post_strategy_analyze(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")

    results_path = strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json"
    if not results_path.is_file():
        raise HTTPException(
            409,
            f"no fwbg results for {s.slug}; run /strategies/{strategy_id}/run first",
        )

    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="analyst",
        status=AgentRunStatus.PENDING.value,
        strategy_id=strategy_id,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    background_tasks.add_task(_run_analyst_background, strategy_id)
    return {"strategy_id": strategy_id, "agent_run_id": ar.id, "status": "scheduled"}


@router.get("/agents/runs")
async def list_agent_runs(
    status: str | None = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List agent runs, newest first. `status` is a comma-separated filter."""
    limit = max(1, min(limit, 100))
    q = select(AgentRun).order_by(desc(AgentRun.created_at)).limit(limit)
    if status:
        statuses = [s.strip() for s in status.split(",")]
        q = q.where(AgentRun.status.in_(statuses))
    rows = (await session.execute(q)).scalars().all()
    return {"runs": [_serialize_agent_run(r) for r in rows]}


@router.get("/agents/runs/{agent_run_id}")
async def get_agent_run(
    agent_run_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")
    return _serialize_agent_run(ar)
