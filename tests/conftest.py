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


def _db_only_live_catalog():
    """An async fetch_live_catalog stand-in that builds a DB-only LiveCatalog
    (agent-authored plugins merged in, empty API catalog) — the API-only
    equivalent of the retired empty-fwbg-repo test setup. No network."""
    from sqlalchemy import select

    from fwbg_agents.orchestrator.live_catalog import LiveCatalog
    from fwbg_agents.orchestrator.plugin_catalog import merge_with_db
    from fwbg_agents.persistence.models import Plugin

    async def _fake(session, fwbg):
        db_plugins = list((await session.execute(select(Plugin))).scalars().all())
        return LiveCatalog(catalog=merge_with_db({}, db_plugins), plugin_details={})

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
