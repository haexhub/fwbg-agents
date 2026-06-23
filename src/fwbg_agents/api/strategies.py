"""Read-only strategy endpoints.

M2 surfaces strategies and their transition history. No create/update/delete:
strategies are produced by the Runner (M3) and the Researcher (M4), never by
direct user input. The dashboard reads from these endpoints; the orchestrator
calls `transition_strategy` directly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.database import get_session
from fwbg_agents.persistence.models import (
    EntityType,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)

router = APIRouter(tags=["strategies"])


def _serialize_strategy(s: Strategy, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": s.id,
        "slug": s.slug,
        "current_state": s.current_state,
        "iteration_count": s.iteration_count,
        "parent_strategy_id": s.parent_strategy_id,
        "asset_class": s.asset_class,
        "strategy_family": s.strategy_family,
        "hypothesis_path": s.hypothesis_path,
        "spec_path": s.spec_path,
        "post_mortem_path": s.post_mortem_path,
        "tags": tags or [],
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
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


@router.get("/strategies")
async def list_strategies(
    state: str | None = None,
    asset_class: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List strategies, optionally filtered by `state` and/or `asset_class`."""
    limit = max(1, min(limit, 500))
    stmt = select(Strategy).order_by(desc(Strategy.created_at)).limit(limit)
    if state:
        try:
            StrategyState(state)  # validate against enum
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid state: {state}") from exc
        stmt = stmt.where(Strategy.current_state == state)
    if asset_class:
        stmt = stmt.where(Strategy.asset_class == asset_class)
    rows = (await session.execute(stmt)).scalars().all()
    return {"strategies": [_serialize_strategy(s) for s in rows]}


@router.get("/strategies/{strategy_id}")
async def get_strategy(
    strategy_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
    tags = (
        await session.execute(select(StrategyTag.tag).where(StrategyTag.strategy_id == strategy_id))
    ).scalars().all()
    transitions = (
        await session.execute(
            select(Transition)
            .where(
                (Transition.entity_type == EntityType.STRATEGY.value)
                & (Transition.entity_id == strategy_id)
            )
            .order_by(asc(Transition.id))
        )
    ).scalars().all()
    return {
        "strategy": _serialize_strategy(s, tags=list(tags)),
        "transitions": [_serialize_transition(t) for t in transitions],
    }


@router.get("/strategies/{strategy_id}/transitions")
async def list_strategy_transitions(
    strategy_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Transition)
            .where(
                (Transition.entity_type == EntityType.STRATEGY.value)
                & (Transition.entity_id == strategy_id)
            )
            .order_by(asc(Transition.id))
        )
    ).scalars().all()
    return {"transitions": [_serialize_transition(t) for t in rows]}
