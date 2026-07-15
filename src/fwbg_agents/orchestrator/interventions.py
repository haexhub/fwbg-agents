"""Interventions digest — aggregated Sharpe deltas for iteration levers (Plan 010 WP5).

Counterpart to `lessons.py` (which records *failed* lines): for every
parent -> child edge in the lineage whose child was created by a recorded
Analyst recommendation (`tune_params` / `change_exit` / `modify_plugins` /
`add_indicator`), this looks up the recommendation kind and the median
Sharpe (across assets) of both parent and child, then aggregates the delta
by `(strategy_family, kind)`. The result is a length-capped digest for the
`{{ interventions_digest }}` prompt slot, so the Analyst sees e.g. "for
other mean_reversion lines, a change_exit intervention historically moved
median Sharpe by +0.3" before picking its own lever.

`regenerate_interventions_digest` recomputes from scratch every call (a
DB query + a handful of small JSON reads per strategy) — idempotent, so it
can be called opportunistically (here: once per Analyst run) without a
dedicated trigger point, mirroring how `trade_diagnostics.md` is freshly
computed per run rather than incrementally maintained.
"""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.models import Strategy, Transition

log = logging.getLogger(__name__)

INTERVENTIONS_FILENAME = "interventions.md"
DIGEST_MAX_CHARS = 4000


def _interventions_path() -> Path:
    return settings.data_dir / INTERVENTIONS_FILENAME


def _median_sharpe(slug: str) -> float | None:
    """Median Sharpe across assets from a strategy's iteration_001 backtest."""
    path = strategy_dir(slug) / "iteration_001" / "fwbg_results.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    values = [
        m["sharpe"]
        for asset in (data.get("assets") or {}).values()
        if isinstance(asset, dict)
        for m in [asset.get("unified_metrics") or {}]
        if isinstance(m.get("sharpe"), (int, float))
    ]
    return statistics.median(values) if values else None


async def _recommendation_kind(session: AsyncSession, child_id: int) -> str | None:
    """The recommendation kind that created `child_id`, from its creation Transition."""
    tr = (
        await session.execute(
            select(Transition)
            .where(
                Transition.entity_type == "strategy",
                Transition.entity_id == child_id,
                Transition.created_by == "translator",
            )
            .order_by(Transition.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if tr is None:
        return None
    payload = tr.payload or {}
    rec = payload.get("recommendation")
    kind = rec.get("kind") if isinstance(rec, dict) else None
    if isinstance(kind, str):
        return kind
    # run_reiterate_with_plugin transitions carry no recommendation dict — the
    # plugin_slug marker identifies the add_indicator lever that spawned them.
    if "plugin_slug" in payload:
        return "add_indicator"
    return None


async def regenerate_interventions_digest(session: AsyncSession) -> Path:
    """Rebuild `data/interventions.md` from every parent -> child edge with a
    known recommendation kind and readable Sharpe for both ends.

    Best-effort: a strategy without a readable backtest or an unrecognized
    Transition payload is simply excluded, never raised.
    """
    children = (
        (await session.execute(select(Strategy).where(Strategy.parent_strategy_id.isnot(None))))
        .scalars()
        .all()
    )
    parents_by_id = {
        s.id: s
        for s in (
            await session.execute(
                select(Strategy).where(Strategy.id.in_({c.parent_strategy_id for c in children}))
            )
        )
        .scalars()
        .all()
    }

    deltas: dict[tuple[str, str], list[float]] = {}
    for child in children:
        if child.parent_strategy_id is None:
            continue  # excluded by the query filter above; narrows the type
        kind = await _recommendation_kind(session, child.id)
        if kind is None:
            continue
        parent = parents_by_id.get(child.parent_strategy_id)
        if parent is None:
            continue
        parent_sharpe = _median_sharpe(parent.slug)
        child_sharpe = _median_sharpe(child.slug)
        if parent_sharpe is None or child_sharpe is None:
            continue
        family = child.strategy_family or "unknown"
        deltas.setdefault((family, kind), []).append(child_sharpe - parent_sharpe)

    lines = ["# Interventions digest — median Sharpe delta by family x lever", ""]
    if not deltas:
        lines.append("(no completed interventions with readable backtests yet)")
    else:
        for (family, kind), values in sorted(deltas.items()):
            median_delta = statistics.median(values)
            lines.append(
                f"- {family} x {kind}: median Δsharpe={median_delta:+.2f} (n={len(values)})"
            )

    path = _interventions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def interventions_digest(max_chars: int = DIGEST_MAX_CHARS) -> str:
    """Length-capped `data/interventions.md` for the Analyst prompt slot."""
    path = _interventions_path()
    if not path.is_file():
        return "(no interventions digest yet — run an Analyst pass to generate one)"
    try:
        return path.read_text()[:max_chars]
    except OSError:
        return "(interventions digest unreadable)"
