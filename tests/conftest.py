"""Shared test fixtures/helpers."""

from __future__ import annotations

import importlib

import pytest

# Modules that import `fetch_live_catalog` at module scope and therefore need it
# patched when a test drives a flow without wiring a real FwbgClient.
_LIVE_CATALOG_CONSUMERS = (
    "fwbg_agents.agents.analyst",
    "fwbg_agents.agents.translator",
    "fwbg_agents.orchestrator.plugin_flow",
)


_TEST_KIND_TO_CATEGORY: dict[str, str] = {
    "indicator": "indicators",
    "model": "models",
    "filter": "filters",
    "exit_strategy": "exit_strategies",
    "risk_management": "filters",
    "entry_modifier": "entry_modifier",
    "feature_selection": "feature_selection",
    "preprocessing": "preprocessing",
    "data_loading": "data_loading",
}


def _db_only_live_catalog():
    """An async fetch_live_catalog stand-in that builds a catalog from DB plugins.

    Simulates what fwbg returns after VERIFIED plugins are registered via
    POST /api/plugins (Phase 3.2): VERIFIED/ADOPTED DB plugins appear in the
    catalog as if GET /api/plugins returned them. No network required.
    """
    from pathlib import Path

    from sqlalchemy import select

    from fwbg_agents.orchestrator.live_catalog import LiveCatalog
    from fwbg_agents.orchestrator.plugin_catalog import (
        PluginCatalog,
        PluginManifest,
    )
    from fwbg_agents.persistence.models import Plugin, PluginState

    _visible = frozenset({PluginState.VERIFIED.value, PluginState.ADOPTED_IN_FWBG.value})

    async def _fake(session, fwbg):
        db_plugins = list((await session.execute(select(Plugin))).scalars().all())
        by_category: dict[str, dict[str, PluginManifest]] = {}
        for p in db_plugins:
            if p.current_state not in _visible:
                continue
            category = _TEST_KIND_TO_CATEGORY.get(p.kind, p.kind)
            spec = Path(p.spec_path) if p.spec_path else Path("")
            by_category.setdefault(category, {})[p.slug] = PluginManifest(
                name=p.slug,
                category=category,
                provenance="agent-authored",
                version=spec.parent.name if p.spec_path else "v1",
                source_path=spec,
            )
        return LiveCatalog(catalog=PluginCatalog(by_category=by_category), plugin_details={})

    return _fake


@pytest.fixture
def patch_live_catalog(monkeypatch):
    """Patch `fetch_live_catalog` to a hermetic DB-only stub across every module
    that imports it. Opt in from a test or fixture that drives a catalog-using
    flow without a live fwbg API."""
    fake = _db_only_live_catalog()
    for modpath in _LIVE_CATALOG_CONSUMERS:
        mod = importlib.import_module(modpath)
        if hasattr(mod, "fetch_live_catalog"):
            monkeypatch.setattr(mod, "fetch_live_catalog", fake)
    return fake
