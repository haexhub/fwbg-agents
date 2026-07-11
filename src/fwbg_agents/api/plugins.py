"""Plugin endpoints — read-only listings plus M5b author / evaluate flows.

The author + evaluate POSTs follow the M3/M4 envelope: pre-create an AgentRun
in PENDING, schedule a BackgroundTask that opens its own SessionLocal,
and return `{agent_run_id, status: "scheduled", ...}` so the caller can poll
the run row.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.plugin_flow import (
    ReiterateWithPluginPreconditionError,
    _find_latest_sidecar,
    author_plugin_from_strategy,
    evaluate_plugin,
    lookup_plugin_capability,
    reiterate_with_plugin,
)
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
    use_parent_run,
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
    """Serialize a Plugin ORM row to a response dict."""
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
    """Serialize a VerificationRun ORM row to a response dict."""
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
    """Serialize a Transition ORM row to a response dict."""
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
    """List all plugins, optionally filtered by state and/or kind."""
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
async def get_plugin(
    plugin_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Retrieve a plugin by ID along with its transition history."""
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
    """List all lifecycle transitions for a plugin."""
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
    """Run the plugin author task in the background."""
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        try:
            with use_parent_run(agent_run_id):
                plugin_id = await author_plugin_from_strategy(session, strategy_id)
            plugin = (
                await session.execute(select(Plugin).where(Plugin.id == plugin_id))
            ).scalar_one()
            await finish_agent_run(
                session,
                ar,
                status=AgentRunStatus.DONE,
                plugin_id=plugin_id,
                output_artifact_path=plugin.contract_path,
            )
        except Exception as exc:
            log.exception("author background task failed (agent_run %s)", agent_run_id)
            await fail_agent_run(session, ar, exc)


async def _run_evaluator_background(plugin_id: int, agent_run_id: int) -> None:
    """Run the plugin evaluator task in the background."""
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
            await finish_agent_run(
                session,
                ar,
                status=AgentRunStatus.DONE,
                plugin_id=plugin_id,
                output_artifact_path=vr.error_log_path,  # None on success
            )
        except Exception as exc:
            log.exception("evaluate background task failed (agent_run %s)", agent_run_id)
            await fail_agent_run(session, ar, exc)


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

    sidecar = _find_latest_sidecar(strategy.slug)
    if sidecar is None:
        raise HTTPException(
            422,
            f"no add_indicator_request.json found under iteration_NNN/ for "
            f"{strategy.slug}; run /strategies/{strategy_id}/analyze first",
        )

    ar = await start_agent_run(
        session,
        agent_name="plugin_author_flow",
        strategy_id=strategy.id,
        input_artifact_path=str(sidecar),
        status=AgentRunStatus.PENDING,
    )

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

    ar = await start_agent_run(
        session,
        agent_name="plugin_evaluator_flow",
        plugin_id=plugin.id,
        status=AgentRunStatus.PENDING,
    )

    background_tasks.add_task(_run_evaluator_background, plugin.id, ar.id)
    return {
        "agent_run_id": ar.id,
        "plugin_id": plugin.id,
        "status": "scheduled",
        "message": f"evaluating {plugin.slug}; poll /agents/runs/{ar.id}",
    }


# ---------------------------------------------------------------------------
# M5c: reiterate-with-plugin background flow
# ---------------------------------------------------------------------------


class ReiterateWithPluginRequest(BaseModel):
    """Request body for POST /strategies/{id}/reiterate-with-plugin."""

    plugin_slug: str


async def _run_reiterate_with_plugin_background(
    strategy_id: int, plugin_slug: str, agent_run_id: int
) -> None:
    """Run the reiterate-with-plugin translator task in the background."""
    from fwbg_agents.orchestrator.lifecycle import strategy_dir

    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()
        try:
            with use_parent_run(agent_run_id):
                child_id = await reiterate_with_plugin(session, strategy_id, plugin_slug)
            child = (
                await session.execute(select(Strategy).where(Strategy.id == child_id))
            ).scalar_one()
            await finish_agent_run(
                session,
                ar,
                status=AgentRunStatus.DONE,
                output_artifact_path=str(
                    strategy_dir(child.slug) / "iteration_001" / "strategy.json"
                ),
            )
        except Exception as exc:
            log.exception(
                "reiterate-with-plugin background task failed (agent_run %s)",
                agent_run_id,
            )
            await fail_agent_run(session, ar, exc)


@router.post("/strategies/{strategy_id}/reiterate-with-plugin", status_code=202)
async def post_strategy_reiterate_with_plugin(
    strategy_id: int,
    body: ReiterateWithPluginRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Splice a VERIFIED plugin into a child Strategy via the Translator.

    202 + AgentRun envelope. 404 if strategy is missing; 422 for any other
    precondition failure (parent not BACKTESTED, plugin missing/not VERIFIED,
    no sidecar, capability mismatch).
    """
    # Run preconditions eagerly so we can return 4xx synchronously.
    try:
        # We don't call reiterate_with_plugin here (it would actually run the
        # Translator). Re-do the cheap checks; the background task re-runs the
        # full precondition list inside a fresh session.
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == strategy_id))
        ).scalar_one_or_none()
        if parent is None:
            raise ReiterateWithPluginPreconditionError(
                f"strategy {strategy_id} not found"
            )
        if parent.current_state != StrategyState.BACKTESTED.value:
            raise ReiterateWithPluginPreconditionError(
                f"strategy {parent.slug} is in state {parent.current_state!r}; "
                "reiterate-with-plugin requires BACKTESTED"
            )
        plugin = (
            await session.execute(select(Plugin).where(Plugin.slug == body.plugin_slug))
        ).scalar_one_or_none()
        if plugin is None:
            raise ReiterateWithPluginPreconditionError(
                f"plugin {body.plugin_slug!r} not found"
            )
        if plugin.current_state != PluginState.VERIFIED.value:
            raise ReiterateWithPluginPreconditionError(
                f"plugin {plugin.slug} is in state {plugin.current_state!r}; "
                "reiterate-with-plugin requires VERIFIED"
            )
        sidecar_path = _find_latest_sidecar(parent.slug)
        if sidecar_path is None:
            raise ReiterateWithPluginPreconditionError(
                f"no add_indicator_request.json found for {parent.slug}"
            )
        # Capability guard (matches plugin_flow.reiterate_with_plugin step 7).
        try:
            parent_cap = json.loads(sidecar_path.read_text()).get("capability")
        except (OSError, json.JSONDecodeError) as exc:
            raise ReiterateWithPluginPreconditionError(
                f"cannot parse sidecar at {sidecar_path}: {exc}"
            ) from exc
        plugin_cap = await lookup_plugin_capability(session, plugin.id)
        if plugin_cap is None or plugin_cap != parent_cap:
            raise ReiterateWithPluginPreconditionError(
                f"plugin {plugin.slug} capability={plugin_cap!r} does "
                f"not match sidecar capability={parent_cap!r}"
            )
    except ReiterateWithPluginPreconditionError as exc:
        msg = str(exc)
        if msg.startswith(f"strategy {strategy_id} not found"):
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    ar = await start_agent_run(
        session,
        agent_name="translator_reiterate_flow",
        strategy_id=parent.id,
        plugin_id=plugin.id,
        input_artifact_path=str(sidecar_path),
        status=AgentRunStatus.PENDING,
    )

    background_tasks.add_task(
        _run_reiterate_with_plugin_background, parent.id, body.plugin_slug, ar.id
    )
    return {
        "agent_run_id": ar.id,
        "strategy_id": parent.id,
        "status": "scheduled",
        "message": (
            f"reiterating {parent.slug} with plugin {body.plugin_slug}; "
            f"poll /agents/runs/{ar.id}"
        ),
    }


@router.get("/plugins/{plugin_id}/verification-runs")
async def list_plugin_verification_runs(
    plugin_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """List all verification runs for a plugin, newest first."""
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
