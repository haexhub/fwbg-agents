"""Live building-block catalog, fetched from the running fwbg API.

The Researcher and Translator must always see the CURRENT set of plugins
(indicators, models, exits, ...) and workspace presets — new plugins are
adopted over time and presets are user-curated, so a frozen in-repo list
goes stale. `fetch_live_catalog` asks fwbg (GET /api/plugins,
/api/exit-modifiers, /api/entry-modifiers, /api/presets/*) on every research
run and merges the result with agent-authored plugins from the DB.

If fwbg is unreachable the loader degrades to the local filesystem scan
(`plugin_catalog.load_catalog`) so research keeps working offline — with a
warning, since the composed strategy may then reference a stale catalog.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
    load_catalog,
    merge_with_db,
)
from fwbg_agents.persistence.models import Plugin
from fwbg_agents.tools.fwbg_client import FwbgClient

log = logging.getLogger(__name__)

# fwbg plugin phase → PluginCatalog category (phases are already plural
# except `model`).
_PHASE_TO_CATEGORY: dict[str, str] = {
    "indicators": "indicators",
    "preprocessing": "preprocessing",
    "feature_selection": "feature_selection",
    "data_loading": "data_loading",
    "exit_strategies": "exit_strategies",
    "risk_management": "risk_management",
    "model": "models",
}

# Preset sections surfaced to the Translator. `validations`/`resources` are
# the operator-curated protocol presets the Translator must pick from;
# the others are only needed to accept legacy string refs.
_PRESET_SECTIONS: tuple[str, ...] = (
    "pipelines",
    "models",
    "filters",
    "validations",
    "resources",
)


class LiveCatalog(BaseModel):
    """Current fwbg building blocks + curated presets, as of one fetch."""

    catalog: PluginCatalog
    # category → [{name, description, default_params}] for prompt rendering.
    plugin_details: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    exit_modifiers: list[dict[str, Any]] = Field(default_factory=list)
    entry_modifiers: list[dict[str, Any]] = Field(default_factory=list)
    # section → available preset names in the fwbg workspace.
    presets: dict[str, list[str]] = Field(default_factory=dict)
    # True when fwbg answered; False on the offline/filesystem fallback.
    from_api: bool = True


def researcher_summary(live: LiveCatalog) -> dict[str, Any]:
    """Compact catalog view for the Researcher prompt: names + descriptions,
    no parameter schemas (the Researcher names capabilities, it doesn't
    write configs)."""

    def _slim(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {"name": e.get("name", ""), "description": e.get("description", "")}
            for e in entries
        ]

    return {
        category: _slim(live.plugin_details.get(category, []))
        for category in ("indicators", "preprocessing", "feature_selection",
                         "data_loading", "models", "exit_strategies")
    } | {
        "exit_modifiers": _slim(live.exit_modifiers),
        "entry_modifiers": _slim(live.entry_modifiers),
    }


def _detail(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry.get("name", ""),
        "description": entry.get("description", ""),
        "default_params": entry.get("defaults", {}) or {},
    }


async def fetch_live_catalog(
    session: AsyncSession, fwbg: FwbgClient | None
) -> LiveCatalog:
    """Fetch the current catalog from fwbg; degrade to filesystem scan offline."""
    if fwbg is not None:
        try:
            return await _fetch_from_api(session, fwbg)
        except Exception:
            log.warning(
                "could not fetch live catalog from fwbg; falling back to "
                "filesystem scan (may be stale)",
                exc_info=True,
            )
    catalog = await load_catalog(session)
    details = {
        category: [{"name": slug, "description": "", "default_params": {}}
                   for slug in catalog.all_slugs_for(category)]
        for category in catalog.by_category
    }
    return LiveCatalog(catalog=catalog, plugin_details=details, from_api=False)


async def _fetch_from_api(session: AsyncSession, fwbg: FwbgClient) -> LiveCatalog:
    plugins = await fwbg.get_plugins()

    by_category: dict[str, dict[str, PluginManifest]] = {}
    details: dict[str, list[dict[str, Any]]] = {}
    for p in plugins:
        category = _PHASE_TO_CATEGORY.get(p.get("phase", ""))
        if category is None:
            continue
        name = p.get("name", "")
        if not name:
            continue
        by_category.setdefault(category, {})[name] = PluginManifest(
            name=name,
            category=category,
            provenance="fwbg-core",
            version=str(p.get("version", "")),
            source_path=".",
        )
        details.setdefault(category, []).append(_detail(p))

    # Agent-authored VERIFIED/ADOPTED plugins shadow fwbg entries, as in the
    # filesystem path.
    db_plugins = list((await session.execute(select(Plugin))).scalars().all())
    catalog = merge_with_db(by_category, db_plugins)

    exit_modifiers = [_detail(m) for m in await fwbg.get_exit_modifiers()]
    entry_modifiers = [_detail(m) for m in await fwbg.get_entry_modifiers()]

    presets: dict[str, list[str]] = {}
    for section in _PRESET_SECTIONS:
        entries = await fwbg.get_presets(section)
        presets[section] = sorted(
            e["id"] for e in entries if isinstance(e, dict) and e.get("id")
        )

    return LiveCatalog(
        catalog=catalog,
        plugin_details=details,
        exit_modifiers=exit_modifiers,
        entry_modifiers=entry_modifiers,
        presets=presets,
    )
