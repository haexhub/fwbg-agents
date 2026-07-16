"""USD economics rollup — read-only (Plan 018).

All USD figures are ESTIMATES at Anthropic list price: the haex-claude-proxy
uses subscription pricing, so these numbers are for relative comparison (cost
per lineage, per outcome, per agent), not billing. Rows whose model is not in
the price table stay unpriced and are surfaced via `unpriced_calls`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.database import get_session
from fwbg_agents.persistence.models import AgentRun, LlmCall, Strategy, StrategyState

router = APIRouter(tags=["economics"])

# Lineage outcome = the most advanced state any strategy in the lineage reached.
_STATE_PRECEDENCE = [
    StrategyState.LIVE_TRADING.value,
    StrategyState.PAPER_TRADING.value,
    StrategyState.BACKTESTED.value,
    StrategyState.PROPOSED.value,
    StrategyState.ABANDONED.value,
]
_PROMOTED_STATES = (StrategyState.PAPER_TRADING.value, StrategyState.LIVE_TRADING.value)


class CostBucket(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LineageCost(BaseModel):
    root_slug: str
    total_cost_usd: float
    outcome: str


class EconomicsSummary(BaseModel):
    """List-price USD estimates over all recorded LLM calls — not billing data."""

    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float  # sum over priced rows
    unpriced_calls: int  # rows with cost_usd NULL — honesty counter
    by_agent: dict[str, CostBucket]  # agent_name -> tokens/cost
    by_outcome: dict[str, CostBucket]  # strategy state -> tokens/cost; "unattributed" bucket
    cost_per_promoted_strategy: float | None  # None if no strategy reached paper/live
    lineage_top: list[LineageCost]  # top 10 most expensive lineages


_SUMS = (
    func.coalesce(func.sum(LlmCall.input_tokens), 0),
    func.coalesce(func.sum(LlmCall.output_tokens), 0),
    func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
)


def _bucket(row) -> CostBucket:
    return CostBucket(input_tokens=int(row[0]), output_tokens=int(row[1]), cost_usd=float(row[2]))


def _root_of(strategy_id: int, parents: dict[int, int | None]) -> int:
    seen: set[int] = set()
    current = strategy_id
    while (parent := parents.get(current)) is not None and current not in seen:
        seen.add(current)
        current = parent
    return current


@router.get("/economics/summary")
async def economics_summary(session: AsyncSession = Depends(get_session)) -> EconomicsSummary:
    """USD rollup at list price — estimates for relative comparison, not billing."""
    totals = (await session.execute(select(*_SUMS))).one()
    unpriced = (
        await session.execute(
            select(func.count()).select_from(LlmCall).where(LlmCall.cost_usd.is_(None))
        )
    ).scalar_one()

    by_agent_rows = await session.execute(
        select(AgentRun.agent_name, *_SUMS)
        .join(AgentRun, LlmCall.agent_run_id == AgentRun.id)
        .group_by(AgentRun.agent_name)
    )
    by_agent = {row[0]: _bucket(row[1:]) for row in by_agent_rows}

    by_outcome_rows = await session.execute(
        select(Strategy.current_state, *_SUMS)
        .select_from(LlmCall)
        .join(AgentRun, LlmCall.agent_run_id == AgentRun.id)
        .outerjoin(Strategy, AgentRun.strategy_id == Strategy.id)
        .group_by(Strategy.current_state)
    )
    by_outcome = {row[0] or "unattributed": _bucket(row[1:]) for row in by_outcome_rows}

    # Per-strategy cost via SQL; lineage resolution via a Python walk over the
    # (small) strategy table only — never over llm_call rows.
    per_strategy_rows = (
        await session.execute(
            select(AgentRun.strategy_id, func.coalesce(func.sum(LlmCall.cost_usd), 0.0))
            .join(AgentRun, LlmCall.agent_run_id == AgentRun.id)
            .where(AgentRun.strategy_id.is_not(None))
            .group_by(AgentRun.strategy_id)
        )
    ).all()
    strategies = (await session.execute(select(Strategy))).scalars().all()
    parents = {s.id: s.parent_strategy_id for s in strategies}
    slugs = {s.id: s.slug for s in strategies}
    lineage_cost: dict[int, float] = {}
    for strategy_id, cost in per_strategy_rows:
        root = _root_of(strategy_id, parents)
        lineage_cost[root] = lineage_cost.get(root, 0.0) + float(cost)
    lineage_states: dict[int, set[str]] = {}
    for s in strategies:
        lineage_states.setdefault(_root_of(s.id, parents), set()).add(s.current_state)

    def _outcome(root: int) -> str:
        states = lineage_states.get(root, set())
        return next((st for st in _STATE_PRECEDENCE if st in states), "unknown")

    lineage_top = [
        LineageCost(
            root_slug=slugs.get(root, f"#{root}"),
            total_cost_usd=cost,
            outcome=_outcome(root),
        )
        for root, cost in sorted(lineage_cost.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ]

    promoted = (
        await session.execute(
            select(func.count())
            .select_from(Strategy)
            .where(Strategy.current_state.in_(_PROMOTED_STATES))
        )
    ).scalar_one()
    total_cost = float(totals[2])

    return EconomicsSummary(
        total_input_tokens=int(totals[0]),
        total_output_tokens=int(totals[1]),
        total_cost_usd=total_cost,
        unpriced_calls=int(unpriced),
        by_agent=by_agent,
        by_outcome=by_outcome,
        cost_per_promoted_strategy=(total_cost / promoted) if promoted else None,
        lineage_top=lineage_top,
    )
