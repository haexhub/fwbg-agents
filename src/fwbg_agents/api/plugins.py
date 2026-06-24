"""Plugin endpoints — read-only listings plus M5b author / evaluate flows.

The author + evaluate POSTs follow the M3/M4 envelope: pre-create an AgentRun
in PENDING, schedule a BackgroundTask that opens its own SessionLocal,
and return `{agent_run_id, status: "scheduled", ...}` so the caller can poll
the run row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.plugin_flow import (
    AuthorPluginPreconditionError,
    EvaluatePluginPreconditionError,
    author_plugin_from_strategy,
    evaluate_plugin,
)
from fwbg_agents.persistence.database import SessionLocal, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    EntityType,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    Transition,
    VerificationRun,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])


def _serialize_plugin(p: Plugin) -> dict[str, Any]:
    return {
        "id": p.id,
        "slug": p.slug,
        "current_state": p.current_state,
        "kind": p.kind,
        "spec_path": p.spec_path,
        "contract_path": p.contract_path,
        "post_mortem_path": p.post_mortem_path,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _serialize_verification_run(vr: VerificationRun) -> dict[str, Any]:
    return {
        "id": vr.id,
        "plugin_id": vr.plugin_id,
        "status": vr.status,
        "scenarios_run": vr.scenarios_run,
        "scenarios_passed": vr.scenarios_passed,
        "error_log_path": vr.error_log_path,
        "started_at": vr.started_at.isoformat() if vr.started_at else None,
        "ended_at": vr.ended_at.isoformat() if vr.ended_at else None,
        "created_at": vr.created_at.isoformat() if vr.created_at else None,
    }


def _serialize_transition(t: Transition) -> dict[str, Any]:
    return {
        "id": t.id,
        "entity_type": t.entity_type,
        "entity_id": t.entity_id,
        "from_state": t.from_state,
        "to_state": t.to_state,
        "reason": t.reason,
        "payload": t.payload,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@router.get("/plugins")
async def list_plugins(
    state: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    stmt = select(Plugin).order_by(desc(Plugin.created_at)).limit(limit)
    if state:
        try:
            PluginState(state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid state: {state}") from exc
        stmt = stmt.where(Plugin.current_state == state)
    if kind:
        stmt = stmt.where(Plugin.kind == kind)
    rows = (await session.execute(stmt)).scalars().all()
    return {"plugins": [_serialize_plugin(p) for p in rows]}


@router.get("/plugins/{plugin_id}")
async def get_plugin(plugin_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    p = (await session.execute(select(Plugin).where(Plugin.id == plugin_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail=f"plugin {plugin_id} not found")
    transitions = (
        await session.execute(
            select(Transition)
            .where(
                (Transition.entity_type == EntityType.PLUGIN.value)
                & (Transition.entity_id == plugin_id)
            )
            .order_by(asc(Transition.id))
        )
    ).scalars().all()
    return {
        "plugin": _serialize_plugin(p),
        "transitions": [_serialize_transition(t) for t in transitions],
    }


@router.get("/plugins/{plugin_id}/transitions")
async def list_plugin_transitions(
    plugin_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Transition)
            .where(
                (Transition.entity_type == EntityType.PLUGIN.value)
                & (Transition.entity_id == plugin_id)
            )
            .order_by(asc(Transition.id))
        )
    ).scalars().all()
    return {"transitions": [_serialize_transition(t) for t in rows]}


# ---------------------------------------------------------------------------
# M5b: author + evaluate background flows
# ---------------------------------------------------------------------------


async def _run_author_background(strategy_id: int, agent_run_id: int) -> None:
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        try:
            plugin_id = await author_plugin_from_strategy(session, strategy_id)
            plugin = (
                await session.execute(select(Plugin).where(Plugin.id == plugin_id))
            ).scalar_one()
            ar.status = AgentRunStatus.DONE.value
            ar.plugin_id = plugin_id
            ar.output_artifact_path = plugin.contract_path
            ar.ended_at = datetime.now(UTC)
            await session.commit()
        except Exception as exc:
            log.exception("author background task failed (agent_run %s)", agent_run_id)
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await session.commit()


async def _run_evaluator_background(plugin_id: int, agent_run_id: int) -> None:
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        try:
            vr_id = await evaluate_plugin(session, plugin_id)
            vr = (
                await session.execute(
                    select(VerificationRun).where(VerificationRun.id == vr_id)
                )
            ).scalar_one()
            ar.status = AgentRunStatus.DONE.value
            ar.plugin_id = plugin_id
            ar.output_artifact_path = vr.error_log_path  # None on success
            ar.ended_at = datetime.now(UTC)
            await session.commit()
        except Exception as exc:
            log.exception("evaluate background task failed (agent_run %s)", agent_run_id)
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await session.commit()


@router.post("/strategies/{strategy_id}/author-plugin", status_code=202)
async def post_strategy_author_plugin(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off PluginAuthor for a strategy with an add_indicator_request sidecar."""
    strategy = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if strategy is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")
    if strategy.current_state != StrategyState.BACKTESTED.value:
        raise HTTPException(
            422,
            f"strategy {strategy.slug} is in state {strategy.current_state!r}; "
            "author-plugin requires BACKTESTED",
        )

    from fwbg_agents.orchestrator.plugin_flow import _find_latest_sidecar
    sidecar = _find_latest_sidecar(strategy.slug)
    if sidecar is None:
        raise HTTPException(
            422,
            f"no add_indicator_request.json found under iteration_NNN/ for "
            f"{strategy.slug}; run /strategies/{strategy_id}/analyze first",
        )

    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="plugin_author_flow",
        status=AgentRunStatus.PENDING.value,
        strategy_id=strategy.id,
        input_artifact_path=str(sidecar),
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    background_tasks.add_task(_run_author_background, strategy.id, ar.id)
    return {
        "agent_run_id": ar.id,
        "strategy_id": strategy.id,
        "status": "scheduled",
        "message": f"authoring plugin for {strategy.slug}; poll /agents/runs/{ar.id}",
    }


@router.post("/plugins/{plugin_id}/evaluate", status_code=202)
async def post_plugin_evaluate(
    plugin_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off PluginEvaluator for a plugin in AUTHORED state."""
    plugin = (
        await session.execute(select(Plugin).where(Plugin.id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise HTTPException(404, f"plugin {plugin_id} not found")
    if plugin.current_state != PluginState.AUTHORED.value:
        raise HTTPException(
            422,
            f"plugin {plugin.slug} is in state {plugin.current_state!r}; "
            "evaluate requires AUTHORED",
        )

    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="plugin_evaluator_flow",
        status=AgentRunStatus.PENDING.value,
        plugin_id=plugin.id,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    background_tasks.add_task(_run_evaluator_background, plugin.id, ar.id)
    return {
        "agent_run_id": ar.id,
        "plugin_id": plugin.id,
        "status": "scheduled",
        "message": f"evaluating {plugin.slug}; poll /agents/runs/{ar.id}",
    }


@router.get("/plugins/{plugin_id}/verification-runs")
async def list_plugin_verification_runs(
    plugin_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    plugin = (
        await session.execute(select(Plugin).where(Plugin.id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise HTTPException(404, f"plugin {plugin_id} not found")
    rows = (
        await session.execute(
            select(VerificationRun)
            .where(VerificationRun.plugin_id == plugin_id)
            .order_by(desc(VerificationRun.created_at))
        )
    ).scalars().all()
    return {"verification_runs": [_serialize_verification_run(vr) for vr in rows]}
