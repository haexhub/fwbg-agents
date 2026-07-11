"""Live building-block catalog, fetched from the running fwbg API.

The Researcher and Translator must always see the CURRENT set of plugins
(indicators, models, exits, ...) and workspace presets — new plugins are
adopted over time and presets are user-curated, so a frozen in-repo list
goes stale. `fetch_live_catalog` asks fwbg (GET /api/plugins,
/api/exit-modifiers, /api/entry-modifiers, /api/presets/*) on every research
run. Agent-authored plugins appear here after being registered with fwbg
via POST /api/plugins (Phase 3.2).

fwbg is the single source of truth: there is NO filesystem fallback. If the
API is unreachable the error propagates so the caller fails loudly rather than
composing a strategy against a stale catalog.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
)
from fwbg_agents.tools.fwbg_client import FwbgClient

log = logging.getLogger(__name__)

# fwbg plugin phase → PluginCatalog category (phases are already plural
# except `model`). fwbg has no `filters` phase — its filter/position-sizing
# plugins carry phase `risk_management`, but the validator queries the catalog
# category `filters` (for `extra_filters`), so we route risk_management→filters.
_PHASE_TO_CATEGORY: dict[str, str] = {
    "indicators": "indicators",
    "preprocessing": "preprocessing",
    "feature_selection": "feature_selection",
    "data_loading": "data_loading",
    "exit_strategies": "exit_strategies",
    "risk_management": "filters",
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
    # Datasources actually configured in fwbg — a strategy referencing any
    # other name cannot be backtested. [{name, assets: [{symbol, timeframes}]}]
    # The per-source asset lists are CURRENT downloads only; anything from
    # `asset_registry` can be fetched on demand (POST /api/data/ensure).
    datasources: list[dict[str, Any]] = Field(default_factory=list)
    # fwbg's asset registry: asset_class → [{symbol, history_start?}].
    # Historical data is downloaded on demand from the connected providers —
    # the research universe is NOT limited to already-downloaded files.
    # history_start (per granularity: minute/hourly/daily) shows how deep the
    # available history goes, e.g. EURUSD daily since 1973, minute since 2003.
    asset_registry: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # Timeframes fwbg supports (MINUTE_1 … DAY_1), fetched live. Empty when
    # the endpoint is unavailable — validation is then lax.
    timeframes: list[str] = Field(default_factory=list)
    # Always True now that fwbg is the sole source (no filesystem fallback);
    # kept for backward compatibility with readers that still check it.
    from_api: bool = True

    def datasource_names(self) -> list[str]:
        """Return the names of all configured fwbg datasources."""
        return [d["name"] for d in self.datasources if d.get("name")]


def researcher_summary(live: LiveCatalog) -> dict[str, Any]:
    """Compact catalog view for the Researcher prompt: names + descriptions,
    no parameter schemas (the Researcher names capabilities, it doesn't
    write configs)."""

    def _slim(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Reduce catalog entries to name/description pairs for the Researcher prompt."""
        return [
            {"name": e.get("name", ""), "description": e.get("description", "")} for e in entries
        ]

    return {
        category: _slim(live.plugin_details.get(category, []))
        for category in (
            "indicators",
            "preprocessing",
            "feature_selection",
            "data_loading",
            "models",
            "exit_strategies",
        )
    } | {
        "exit_modifiers": _slim(live.exit_modifiers),
        "entry_modifiers": _slim(live.entry_modifiers),
        # The testable universe: every registry symbol can be backtested —
        # historical data is fetched on demand from the connected providers.
        "asset_registry": live.asset_registry,
        "datasources": live.datasources,
    }


def _detail(entry: dict[str, Any]) -> dict[str, Any]:
    """Extract name, fqn, description, and default params from a catalog entry dict."""
    return {
        "name": entry.get("name", ""),
        # fqn is the API's stable plugin id; carried so the PluginPlanner can
        # fetch example source via GET /api/plugins/{fqn}/source. Empty for
        # entry/exit modifiers, which are not fetched as source examples.
        "fqn": entry.get("fqn", ""),
        "description": entry.get("description", ""),
        "default_params": entry.get("defaults", {}) or {},
    }


async def fetch_live_catalog(session: AsyncSession, fwbg: FwbgClient | None) -> LiveCatalog:
    """Fetch the current catalog from fwbg. fwbg is the single source of truth.

    Raises if no client is configured or the API is unreachable — there is no
    filesystem fallback (a stale catalog would silently corrupt composition).
    """
    if fwbg is None:
        raise RuntimeError(
            "fetch_live_catalog requires a FwbgClient; there is no offline filesystem fallback"
        )
    return await _fetch_from_api(session, fwbg)


async def _fetch_from_api(session: AsyncSession, fwbg: FwbgClient) -> LiveCatalog:
    """Fetch and assemble the full LiveCatalog from the fwbg HTTP API."""
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
            source_path=Path("."),
        )
        details.setdefault(category, []).append(_detail(p))

    catalog = PluginCatalog(by_category=by_category)

    exit_modifiers = [_detail(m) for m in await fwbg.get_exit_modifiers()]
    entry_modifiers = [_detail(m) for m in await fwbg.get_entry_modifiers()]

    presets: dict[str, list[str]] = {}
    for section in _PRESET_SECTIONS:
        entries = await fwbg.get_presets(section)
        presets[section] = sorted(e["id"] for e in entries if isinstance(e, dict) and e.get("id"))

    datasources = await _fetch_datasources(fwbg)

    # History depth per symbol from the Dukascopy catalogue; tolerated as
    # missing (older fwbg without the endpoint, or catalogue unavailable).
    history_by_symbol: dict[str, dict[str, Any]] = {}
    try:
        for inst in await fwbg.get_dukascopy_instruments():
            if inst.get("symbol") and inst.get("historyStart"):
                history_by_symbol[inst["symbol"]] = inst["historyStart"]
    except Exception:
        log.warning("could not fetch instrument catalogue; omitting history depth")

    registry: dict[str, list[dict[str, Any]]] = {}
    for asset in await fwbg.get_assets():
        cls, symbol = asset.get("asset_class"), asset.get("symbol")
        if not cls or not symbol:
            continue
        entry: dict[str, Any] = {"symbol": symbol}
        if symbol in history_by_symbol:
            entry["history_start"] = history_by_symbol[symbol]
        registry.setdefault(cls, []).append(entry)

    try:
        timeframes = await fwbg.get_timeframes()
    except Exception:
        log.warning("could not fetch timeframes; validation will be lax")
        timeframes = []

    return LiveCatalog(
        catalog=catalog,
        plugin_details=details,
        exit_modifiers=exit_modifiers,
        entry_modifiers=entry_modifiers,
        presets=presets,
        datasources=datasources,
        asset_registry={k: sorted(v, key=lambda e: e["symbol"]) for k, v in registry.items()},
        timeframes=timeframes,
    )


async def _fetch_datasources(fwbg: FwbgClient) -> list[dict[str, Any]]:
    """Configured datasources + what data each actually has, so the Translator
    picks a datasource/timeframe a backtest can run on."""
    sources = await fwbg.get_datasources()
    try:
        availability = await fwbg.get_datasource_assets()
        assets = availability.get("assets", [])
    except Exception:
        log.warning("could not fetch datasource assets; listing names only")
        assets = []

    by_source: dict[str, list[dict[str, Any]]] = {}
    for a in assets:
        source = a.get("source")
        if source:
            by_source.setdefault(source, []).append(
                {"symbol": a.get("symbol"), "timeframes": a.get("timeframes", [])}
            )
    return [
        {"name": s["name"], "assets": by_source.get(s["name"], [])}
        for s in sources
        if s.get("name")
    ]
