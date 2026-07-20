"""Lineage boundary: one frozen in-sample/holdout split per strategy family.

Plan 014. Before this module, the holdout window was recomputed from
``date.today()`` on every promote-gate run, while iteration backtests capped
their data at ``today - holdout_months`` computed at *their own* run time.
There was no single frozen boundary shared across a lineage's iterations, so
different generations of the same family effectively judged themselves
against slightly different (and drifting) holdout windows.

``get_or_freeze_boundary`` fixes a single date ``B`` the first time any
strategy in a lineage needs one, and persists it as a sidecar at the lineage
root's directory. From then on:

- iteration backtests train/test on ``[..., B)`` (exclusive upper bound),
- the promote gate's holdout backtest runs on ``[B, today)`` (inclusive lower
  bound),

so no iteration of the lineage ever sees the holdout data, no matter how many
generations later the promote gate runs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.models import Strategy

log = logging.getLogger(__name__)


async def lineage_root(session: AsyncSession, strategy: Strategy) -> Strategy:
    """Walk `parent_strategy_id` up to the ancestor with no parent.

    Guards against a cycle (should never happen, but a self-referential FK
    chain could otherwise loop forever): if a parent is revisited, logs an
    error and returns the last non-repeated ancestor found.
    """
    seen = {strategy.id}
    cur = strategy
    while cur.parent_strategy_id is not None:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == cur.parent_strategy_id))
        ).scalar_one_or_none()
        if parent is None:
            break
        if parent.id in seen:
            log.error(
                "lineage_boundary: cycle detected in parent_strategy_id chain at "
                "strategy id=%s (slug=%s); stopping at %s",
                parent.id,
                parent.slug,
                cur.slug,
            )
            break
        seen.add(parent.id)
        cur = parent
    return cur


def boundary_path(root_slug: str) -> Path:
    """Sidecar path for the frozen lineage boundary."""
    return strategy_dir(root_slug) / "lineage_boundary.json"


async def get_or_freeze_boundary(session: AsyncSession, strategy: Strategy) -> str:
    """Return the frozen ISO date `B` for `strategy`'s lineage, freezing it if absent.

    `B` is the exclusive upper bound of in-sample data for the whole lineage,
    and the inclusive lower bound of the holdout. Once frozen, `B` never
    changes for this lineage regardless of how many more iterations run or
    how much later the promote gate is invoked.
    """
    root = await lineage_root(session, strategy)
    path = boundary_path(root.slug)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            data_end = data.get("data_end")
            if isinstance(data_end, str) and data_end:
                return data_end
        except (OSError, json.JSONDecodeError):
            log.warning(
                "lineage_boundary: unreadable sidecar at %s; refreezing", path, exc_info=True
            )

    # Local import: avoids a module-level circular import (runner.py will
    # import get_or_freeze_boundary from this module).
    from fwbg_agents.agents.runner import _months_ago_iso

    data_end = _months_ago_iso(settings.holdout_months)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data_end": data_end}, indent=2))
    return data_end
