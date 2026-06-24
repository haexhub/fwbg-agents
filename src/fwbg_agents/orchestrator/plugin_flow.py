"""Orchestration glue for M5b plugin lifecycle endpoints.

Mirrors `research_flow.research_and_translate` / `research_flow.reiterate`:
each entry point does precondition checks, instantiates the agent, and
returns a single integer id (plugin_id or verification_run_id). API layer
wraps these in BackgroundTasks + AgentRun envelopes.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pydantic_ai.models import Model
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.plugin_author import PluginAuthor
from fwbg_agents.agents.plugin_evaluator import PluginEvaluator
from fwbg_agents.agents.translator import Translator
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.plugin_catalog import _load_fwbg_cached
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
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


class ReiterateWithPluginPreconditionError(RuntimeError):
    """4xx from POST /strategies/{id}/reiterate-with-plugin (404 if the
    message starts with 'strategy ... not found', otherwise 422)."""


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


async def reiterate_with_plugin(
    session: AsyncSession,
    strategy_id: int,
    plugin_slug: str,
) -> int:
    """Splice a VERIFIED plugin into a child Strategy via Translator.

    Preconditions (raise `ReiterateWithPluginPreconditionError`):
      1. parent Strategy exists ("not found" → 404 in API layer).
      2. parent.current_state == BACKTESTED.
      3. plugin (by slug) exists.
      4. plugin.current_state == VERIFIED.
      5. parent has a latest `add_indicator_request.json` sidecar.
      6. parent's sidecar `capability` matches the originating sidecar that
         was used to author this plugin (looked up via the plugin_author
         AgentRun row for `plugin_id`). Guards against splicing a plugin
         into a strategy that asked for a different capability.

    On all checks passing, clears the fwbg-catalog cache (Decision E) and
    invokes `Translator.run_reiterate_with_plugin`. Returns child Strategy id.
    """
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
        await session.execute(select(Plugin).where(Plugin.slug == plugin_slug))
    ).scalar_one_or_none()
    if plugin is None:
        raise ReiterateWithPluginPreconditionError(
            f"plugin {plugin_slug!r} not found"
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

    try:
        sidecar = json.loads(sidecar_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReiterateWithPluginPreconditionError(
            f"cannot parse sidecar at {sidecar_path}: {exc}"
        ) from exc

    parent_capability = sidecar.get("capability")
    plugin_capability = await _lookup_plugin_capability(session, plugin.id)
    if plugin_capability is None or plugin_capability != parent_capability:
        raise ReiterateWithPluginPreconditionError(
            f"plugin {plugin.slug} capability={plugin_capability!r} does "
            f"not match sidecar capability={parent_capability!r}"
        )

    # Decision E: clear the fwbg-catalog process-lifetime cache so a
    # freshly-VERIFIED plugin shows up immediately for the catalog merge.
    _load_fwbg_cached.cache_clear()

    translator = Translator(session)
    child = await translator.run_reiterate_with_plugin(parent, plugin_slug, sidecar)
    return child.id


async def _lookup_plugin_capability(
    session: AsyncSession, plugin_id: int
) -> str | None:
    """Read the originating sidecar's `capability` from the PluginAuthor
    AgentRun for this plugin.

    PluginAuthor.run_fresh sets `ar.plugin_id = plugin.id` and
    `ar.input_artifact_path = str(sidecar_path)`. We pick the most recent
    DONE row to handle re-runs (cf. M5b uniqueness guard at slug-level).
    Returns None when the row or sidecar is missing/unreadable — caller
    treats that as a precondition failure.
    """
    ar = (
        await session.execute(
            select(AgentRun)
            .where(
                (AgentRun.plugin_id == plugin_id)
                & (AgentRun.agent_name == "plugin_author")
                & (AgentRun.status == AgentRunStatus.DONE.value)
            )
            .order_by(desc(AgentRun.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if ar is None or not ar.input_artifact_path:
        return None
    try:
        data = json.loads(Path(ar.input_artifact_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cap = data.get("capability")
    return cap if isinstance(cap, str) else None


__all__ = [
    "AuthorPluginPreconditionError",
    "EvaluatePluginPreconditionError",
    "ReiterateWithPluginPreconditionError",
    "author_plugin_from_strategy",
    "evaluate_plugin",
    "reiterate_with_plugin",
]
