"""Orchestration glue for M5b plugin lifecycle endpoints.

Mirrors `research_flow.research_and_translate` / `research_flow.reiterate`:
each entry point does precondition checks, instantiates the agent, and
returns a single integer id (plugin_id or verification_run_id). API layer
wraps these in BackgroundTasks + AgentRun envelopes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic_ai.models import Model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.plugin_author import PluginAuthor
from fwbg_agents.agents.plugin_evaluator import PluginEvaluator
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.models import (
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
)

log = logging.getLogger(__name__)


class AuthorPluginPreconditionError(RuntimeError):
    """422 from POST /strategies/{id}/author-plugin."""


class EvaluatePluginPreconditionError(RuntimeError):
    """422 from POST /plugins/{id}/evaluate."""


_ITERATION_RE = re.compile(r"^iteration_(\d+)$")


def _find_latest_sidecar(slug: str) -> Path | None:
    """Locate `add_indicator_request.json` from the latest iteration_NNN."""
    sdir = strategy_dir(slug)
    if not sdir.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    for child in sdir.iterdir():
        if not child.is_dir():
            continue
        m = _ITERATION_RE.match(child.name)
        if not m:
            continue
        sidecar = child / "add_indicator_request.json"
        if sidecar.is_file():
            candidates.append((int(m.group(1)), sidecar))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


async def author_plugin_from_strategy(
    session: AsyncSession,
    strategy_id: int,
    *,
    model: Model | None = None,
) -> int:
    """Run PluginAuthor for a strategy that has an add_indicator_request.json
    sidecar in its latest iteration. Returns the new plugin_id."""
    strategy = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if strategy is None:
        raise AuthorPluginPreconditionError(f"strategy {strategy_id} not found")

    if strategy.current_state != StrategyState.BACKTESTED.value:
        raise AuthorPluginPreconditionError(
            f"strategy {strategy.slug} is in state {strategy.current_state!r}; "
            "author-plugin requires BACKTESTED"
        )

    sidecar = _find_latest_sidecar(strategy.slug)
    if sidecar is None:
        raise AuthorPluginPreconditionError(
            f"no add_indicator_request.json found under data/strategies/{strategy.slug}/"
            f"iteration_NNN/; run /strategies/{strategy_id}/analyze first"
        )

    author = PluginAuthor(session, model=model)
    return await author.run_fresh(sidecar_path=sidecar, parent_strategy=strategy)


async def evaluate_plugin(session: AsyncSession, plugin_id: int) -> int:
    """Run PluginEvaluator for a plugin in AUTHORED state.
    Returns the verification_run_id."""
    plugin = (
        await session.execute(select(Plugin).where(Plugin.id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise EvaluatePluginPreconditionError(f"plugin {plugin_id} not found")
    if plugin.current_state != PluginState.AUTHORED.value:
        raise EvaluatePluginPreconditionError(
            f"plugin {plugin.slug} is in state {plugin.current_state!r}; "
            "evaluate requires AUTHORED"
        )

    evaluator = PluginEvaluator(session)
    return await evaluator.run(plugin)


__all__ = [
    "AuthorPluginPreconditionError",
    "EvaluatePluginPreconditionError",
    "author_plugin_from_strategy",
    "evaluate_plugin",
]
