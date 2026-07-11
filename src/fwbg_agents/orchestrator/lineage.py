"""Strategy lineage helpers — generation depth + family history for the Analyst.

Iterations are modeled as child Strategy rows (`parent_strategy_id`), so a
strategy's improvement history is its family tree. The Analyst needs that
history to judge whether the last applied change actually improved anything
(and to stop oscillating between the same two parameter values).

All information is assembled from what already exists:
- the family tree from `strategy.parent_strategy_id`,
- the change that created a child from its creation `Transition`
  (`created_by="translator"`, `payload.recommendation`),
- per-asset backtest metrics from `iteration_001/fwbg_results.json`,
- the member's own analyst verdict from `iteration_001/analyst_recommendation.json`
  / `add_indicator_request.json`,
- abandon lessons from `post_mortem.yaml`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.models import Strategy, Transition

log = logging.getLogger(__name__)

# Metrics shown per asset in the family-history block. Keep this short — the
# Analyst gets the full metrics JSON for the *current* strategy separately.
_SUMMARY_METRIC_KEYS: tuple[str, ...] = (
    "sharpe",
    "max_drawdown",
    "win_rate",
    "total_trades",
    "profit_factor",
)

_IMPROVEMENT_METRIC_KEYS: tuple[str, ...] = ("sharpe", "profit_factor")


def has_metric_improvement(
    metrics_history: list[dict],
    *,
    keys: tuple[str, ...] = _IMPROVEMENT_METRIC_KEYS,
    lookback: int = 3,
) -> bool:
    """True if at least one key metric shows an upward trend in recent iterations.

    metrics_history: ordered list of unified_metrics dicts, oldest → newest.
    lookback: how many of the most recent entries to consider.
    Returns False when fewer than 2 entries with numeric values exist for a key,
    or when no key shows improvement.
    """
    window = metrics_history[-lookback:]
    if len(window) < 2:
        return False
    for key in keys:
        vals = [m.get(key) for m in window]
        numeric = [v for v in vals if isinstance(v, (int, float))]
        if len(numeric) < 2:
            continue
        if numeric[-1] > numeric[0]:
            return True
    return False


async def generation_depth(session: AsyncSession, strategy: Strategy) -> int:
    """1-based depth of `strategy` in its iteration chain (root = 1)."""
    depth = 1
    seen = {strategy.id}
    cur = strategy
    while cur.parent_strategy_id is not None:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == cur.parent_strategy_id))
        ).scalar_one_or_none()
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        depth += 1
        cur = parent
    return depth


async def family_strategies(session: AsyncSession, strategy: Strategy) -> list[Strategy]:
    """Every member of `strategy`'s family (root + all descendants), oldest first."""
    root = strategy
    seen = {strategy.id}
    while root.parent_strategy_id is not None:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == root.parent_strategy_id))
        ).scalar_one_or_none()
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        root = parent

    members: dict[int, Strategy] = {root.id: root}
    frontier = [root.id]
    while frontier:
        children = (
            (
                await session.execute(
                    select(Strategy).where(Strategy.parent_strategy_id.in_(frontier))
                )
            )
            .scalars()
            .all()
        )
        frontier = [c.id for c in children if c.id not in members]
        for c in children:
            members[c.id] = c

    return sorted(members.values(), key=lambda s: (s.created_at, s.id))


def _per_asset_summary(slug: str) -> str | None:
    """Compact 'SYMBOL: sharpe=…, …' line from iteration_001/fwbg_results.json."""
    results_path = strategy_dir(slug) / "iteration_001" / "fwbg_results.json"
    if not results_path.is_file():
        return None
    try:
        results = json.loads(results_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    parts: list[str] = []
    for sym, data in (results.get("assets") or {}).items():
        m = data.get("unified_metrics") or {}
        kv = ", ".join(
            f"{k}={m[k]}" for k in _SUMMARY_METRIC_KEYS if isinstance(m.get(k), (int, float))
        )
        parts.append(f"{sym}({kv})" if kv else f"{sym}(no metrics)")
    return "; ".join(parts) if parts else None


def _rec_summary(rec: dict[str, Any]) -> str:
    """One-line summary of a recommendation dict (sidecar or transition payload)."""
    kind = rec.get("kind", "unknown")
    detail = {
        k: v
        for k, v in rec.items()
        if k not in ("kind", "confidence", "reasoning") and v not in (None, [], {}, "")
    }
    return f"{kind} {json.dumps(detail, default=str)}" if detail else kind


def _analyst_verdict(slug: str) -> str | None:
    """What this member's own analyst run decided after its backtest, if any."""
    iteration_dir = strategy_dir(slug) / "iteration_001"
    for name in ("analyst_recommendation.json", "add_indicator_request.json"):
        path = iteration_dir / name
        if path.is_file():
            try:
                return _rec_summary(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _abandon_lessons(strategy: Strategy) -> list[str]:
    """Extract lessons-learned strings from an abandoned strategy's post-mortem YAML."""
    pm_path = strategy_dir(strategy.slug) / "post_mortem.yaml"
    if not pm_path.is_file():
        return []
    try:
        data = yaml.safe_load(pm_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    lessons = data.get("lessons")
    return [str(x) for x in lessons] if isinstance(lessons, list) else []


async def _applied_change(session: AsyncSession, strategy: Strategy) -> str | None:
    """The recommendation that created `strategy`, from its creation Transition."""
    if strategy.parent_strategy_id is None:
        return None
    tr = (
        await session.execute(
            select(Transition)
            .where(
                Transition.entity_type == "strategy",
                Transition.entity_id == strategy.id,
                Transition.created_by == "translator",
            )
            .order_by(Transition.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if tr is None:
        return None
    rec = (tr.payload or {}).get("recommendation")
    return _rec_summary(rec) if isinstance(rec, dict) else None


async def render_family_history(session: AsyncSession, strategy: Strategy) -> tuple[int, str]:
    """(generation depth of `strategy`, markdown block of the whole family).

    One bullet per family member, oldest first, so the Analyst can compare
    each applied change against the metrics it produced.
    """
    members = await family_strategies(session, strategy)
    depths: dict[int, int] = {}
    for m in members:
        parent_depth = depths.get(m.parent_strategy_id or -1, 0)
        depths[m.id] = parent_depth + 1

    lines: list[str] = []
    for m in members:
        marker = " ← CURRENT" if m.id == strategy.id else ""
        lines.append(f"- generation {depths[m.id]} · `{m.slug}` [{m.current_state}]{marker}")
        applied = await _applied_change(session, m)
        if applied:
            lines.append(f"  - change applied vs parent: {applied}")
        metrics = _per_asset_summary(m.slug)
        if metrics:
            lines.append(f"  - backtest per asset: {metrics}")
        if m.id != strategy.id:
            verdict = _analyst_verdict(m.slug)
            if verdict:
                lines.append(f"  - analyst verdict: {verdict}")
        lessons = _abandon_lessons(m)
        for lesson in lessons:
            lines.append(f"  - lesson: {lesson}")

    depth = depths.get(strategy.id) or await generation_depth(session, strategy)
    if len(members) <= 1:
        return depth, "(first iteration — no prior family history)"
    return depth, "\n".join(lines)
