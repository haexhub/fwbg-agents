"""Strategy endpoints.

M2 added read-only listing + transition history. M3 adds a single creation
path — POST /strategies — used by the manual smoke flow and (later) by the
Researcher agent. Updates / deletes never exist; all post-creation changes
go through the lifecycle module's transition functions.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir, transition_strategy
from fwbg_agents.orchestrator.paper_flow import paper_analyze
from fwbg_agents.persistence.database import SessionLocal, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    EntityType,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)
from fwbg_agents.tools.fwbg_paper_reader import read_paper_positions, read_paper_summary

log = logging.getLogger(__name__)

router = APIRouter(tags=["strategies"])


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,126}[a-z0-9]$")


class StrategyCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=128)
    asset_class: str = Field(min_length=1, max_length=32)
    strategy_family: str = Field(min_length=1, max_length=64)
    strategy_json: dict[str, Any]
    tags: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must match [a-z0-9][a-z0-9_]*[a-z0-9] (3..128 chars)"
            )
        return v


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


@router.post("/strategies", status_code=201)
async def create_strategy(
    body: StrategyCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Create a new strategy in PROPOSED. Writes iteration_001/strategy.json.

    Idempotent against the same slug only insofar as duplicates are rejected
    with 409 — we never overwrite. No transition row is emitted; creation is
    not a state change.
    """
    exists = (
        await session.execute(select(Strategy).where(Strategy.slug == body.slug))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail=f"strategy with slug {body.slug!r} exists")

    now = datetime.now(UTC)
    s = Strategy(
        slug=body.slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class=body.asset_class,
        strategy_family=body.strategy_family,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    try:
        await session.flush()  # need s.id for tag rows
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"slug conflict: {body.slug}") from exc

    for tag in dict.fromkeys(body.tags):  # dedupe, preserve order
        session.add(StrategyTag(strategy_id=s.id, tag=tag))

    iteration_dir = strategy_dir(body.slug) / "iteration_001"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "strategy.json").write_text(
        json.dumps(body.strategy_json, indent=2, sort_keys=True)
    )

    await session.commit()
    await session.refresh(s)

    return {
        "id": s.id,
        "slug": s.slug,
        "current_state": s.current_state,
        "iteration_dir": str(iteration_dir),
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


def _require_paper_or_live_trading(strategy: Strategy) -> None:
    """Guard for endpoints that only make sense in PAPER_TRADING / LIVE_TRADING.

    Raises 409 with the actual state for debuggability. Extracted so the
    upcoming /paper-positions endpoint (Task 6) can reuse it verbatim.
    """
    allowed = {StrategyState.PAPER_TRADING.value, StrategyState.LIVE_TRADING.value}
    if strategy.current_state not in allowed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"strategy not in PAPER_TRADING or LIVE_TRADING state, "
                f"got {strategy.current_state}"
            ),
        )


@router.get("/strategies/{strategy_id}/paper-summary")
async def get_paper_summary(
    strategy_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Return on-disk paper-trade telemetry. Polled by the dashboard. No LLM."""
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
    _require_paper_or_live_trading(s)
    summary = read_paper_summary(s.slug, settings.fwbg_data_dir)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail=f"no paper-trade data on disk for strategy {s.slug}",
        )
    return summary.model_dump(mode="json")


@router.get("/strategies/{strategy_id}/paper-positions")
async def get_paper_positions(
    strategy_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Return currently-open positions (with SL/TP) for dashboard live-view. No LLM."""
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
    _require_paper_or_live_trading(s)
    positions = read_paper_positions(s.slug, settings.fwbg_data_dir)
    if positions is None:
        raise HTTPException(
            status_code=404,
            detail=f"no positions snapshot on disk for strategy {s.slug}",
        )
    return positions.model_dump(mode="json")


# ---------------------------------------------------------------------------
# M6b: paper-analyze background flow (manual analyst trigger)
# ---------------------------------------------------------------------------


class PaperAnalyzeResponse(BaseModel):
    agent_run_id: int
    status: str


async def _run_paper_analyze_background(strategy_id: int, agent_run_id: int) -> None:
    """Open a fresh session, load the pre-created AgentRun, run paper_analyze.

    Mirrors `_run_reiterate_with_plugin_background` from M5c — the endpoint
    pre-creates a PENDING AgentRun so the HTTP client can poll immediately;
    this wrapper flips it to RUNNING via paper_analyze(existing_ar=…) and
    catches failures to mark FAILED.
    """
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        try:
            await paper_analyze(strategy_id, session, existing_ar=ar)
        except Exception as exc:
            log.exception(
                "paper-analyze background task failed (agent_run %s)", agent_run_id
            )
            # Defensive: paper_analyze's own except block already marks FAILED + commits before re-raising.
            # This handler covers TOCTOU windows (e.g. state changed between endpoint check and BG-task start)
            # where the row could end up in an inconsistent state.
            await session.refresh(ar)
            if ar.status != AgentRunStatus.FAILED.value:
                ar.status = AgentRunStatus.FAILED.value
                ar.ended_at = datetime.now(UTC)
                ar.error = str(exc)
                await session.commit()


@router.post("/strategies/{strategy_id}/paper-analyze", status_code=202)
async def post_strategy_paper_analyze(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> PaperAnalyzeResponse:
    """Kick PaperAnalyst against on-disk telemetry. 202 + AgentRun envelope.

    404 if the strategy is missing. 422 if it is not in PAPER_TRADING or if
    no on-disk telemetry exists yet. Never transitions state (promote/abandon
    edges require human approval — see lifecycle.py).
    """
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
    if s.current_state != StrategyState.PAPER_TRADING.value:
        raise HTTPException(
            status_code=422,
            detail=(
                f"strategy {s.slug} is in state {s.current_state!r}; "
                "paper-analyze requires PAPER_TRADING"
            ),
        )
    summary = read_paper_summary(s.slug, settings.fwbg_data_dir)
    if summary is None:
        raise HTTPException(
            status_code=422,
            detail=f"no on-disk paper-trade data for strategy {s.slug}",
        )

    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="paper_analyst",
        status=AgentRunStatus.PENDING.value,
        strategy_id=s.id,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    background_tasks.add_task(_run_paper_analyze_background, s.id, ar.id)
    return PaperAnalyzeResponse(agent_run_id=ar.id, status="scheduled")


# ---------------------------------------------------------------------------
# M6b: human-gated promote-live (operator-driven transition to LIVE_TRADING)
# ---------------------------------------------------------------------------


class PromoteLiveBody(BaseModel):
    human_approval: bool
    operator_note: str | None = None


class PromoteLiveResponse(BaseModel):
    strategy_id: int
    new_state: str
    agent_run_id: int


@router.post(
    "/strategies/{strategy_id}/promote-live",
    response_model=PromoteLiveResponse,
    status_code=200,
)
async def post_strategy_promote_live(
    strategy_id: int,
    body: PromoteLiveBody,
    session: AsyncSession = Depends(get_session),
) -> PromoteLiveResponse:
    """Triple-gated promotion from PAPER_TRADING to LIVE_TRADING.

    Gates (all three must pass):
      1. body.human_approval is True              (operator pressed the button)
      2. metadata.paper_analyst_promote_recommended is True  (analyst signed off)
      3. strategy.current_state == PAPER_TRADING  (state-machine edge exists)

    The M2 lifecycle guard `_guard_strategy_paper_to_live` re-checks gate 1's
    payload["human_approval"]==True; the LLM cannot bypass it because the LLM
    is never the HTTP caller for this endpoint — only an operator is.

    No LLM is invoked. The AgentRun row is purely an audit trace.
    """
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")

    if not body.human_approval:
        raise HTTPException(
            status_code=422,
            detail="human_approval must be true to promote to LIVE_TRADING",
        )

    meta = s.metadata_json or {}
    if not meta.get("paper_analyst_promote_recommended"):
        raise HTTPException(
            status_code=422,
            detail=(
                "strategy does not have paper_analyst_promote_recommended=True; "
                "run POST /strategies/{id}/paper-analyze first"
            ),
        )

    if s.current_state != StrategyState.PAPER_TRADING.value:
        raise HTTPException(
            status_code=422,
            detail=(
                f"strategy {s.slug} is in state {s.current_state!r}; "
                "promote-live requires PAPER_TRADING"
            ),
        )

    # Normalize operator_note: empty / whitespace-only → None so downstream
    # consumers can rely on a single sentinel instead of checking both.
    note = (body.operator_note or "").strip() or None

    # Stage the AgentRun BEFORE transition_strategy so both rows flush in the
    # same transaction (transition_strategy's internal commit covers them).
    # If transition_strategy fails for any reason (guard, IO, …), the AgentRun
    # is rolled back as part of the same SQLAlchemy session — no orphan audit.
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="promote_live",
        status=AgentRunStatus.DONE.value,
        strategy_id=s.id,
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)

    # M2 guard re-validates payload["human_approval"]; deferred-defence by design.
    await transition_strategy(
        session,
        s,
        StrategyState.LIVE_TRADING,
        payload={"human_approval": True, "operator_note": note},
        created_by="operator",
    )
    await session.refresh(ar)

    # Clear the stale paper-analyst flag and stamp promoted_live_at for dashboard
    # hygiene. This is a SECOND commit — if it fails, the audit trail (Transition
    # row) is still correct because transition_strategy already committed.
    now_iso = datetime.now(UTC).isoformat()
    meta = dict(s.metadata_json or {})
    meta["paper_analyst_promote_recommended"] = False
    meta["promoted_live_at"] = now_iso
    s.metadata_json = meta
    await session.commit()
    await session.refresh(s)

    return PromoteLiveResponse(
        strategy_id=s.id,
        new_state=StrategyState.LIVE_TRADING.value,
        agent_run_id=ar.id,
    )
