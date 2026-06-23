"""Read-only plugin endpoints. Mirrors `strategies.py`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.database import get_session
from fwbg_agents.persistence.models import EntityType, Plugin, PluginState, Transition

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
