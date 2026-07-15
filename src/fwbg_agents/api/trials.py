"""Trial-count summary — read-only, for external DSR display (Plan 010 WP2).

The promote gate computes its own DSR internally (orchestrator/promote_gate.py)
against the holdout run. This endpoint exposes the same global search-breadth
census so other surfaces (e.g. the fwbg-dashboard generic run view, which has
no visibility into fwbg-agents' strategy database) can compute a DSR for an
arbitrary run using their own locally-available trade data.
"""

from __future__ import annotations

import statistics

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.trials import count_trials
from fwbg_agents.persistence.database import get_session

router = APIRouter(tags=["trials"])


class TrialsSummary(BaseModel):
    """Global search-breadth census as of now (recomputed on every call —
    it's a filesystem scan, not a cached snapshot, so it reflects the latest
    completed backtests)."""

    n_trials: int
    sr_variance_across_trials: float | None  # None below 2 historical samples
    sr_variance_sample_size: int


@router.get("/trials/summary")
async def trials_summary(session: AsyncSession = Depends(get_session)) -> TrialsSummary:
    counts = await count_trials(session)
    variance = statistics.variance(counts.trade_sharpes) if len(counts.trade_sharpes) >= 2 else None
    return TrialsSummary(
        n_trials=counts.global_trials,
        sr_variance_across_trials=variance,
        sr_variance_sample_size=len(counts.trade_sharpes),
    )
