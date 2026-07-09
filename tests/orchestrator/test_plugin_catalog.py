"""Tests for orchestrator.plugin_catalog — shared catalog types.

Agent-authored plugins now reach the catalog via fwbg's API (registered with
POST /api/plugins in Phase 3.2), so there is no DB-merge path to test here.
"""

from __future__ import annotations

from pathlib import Path

from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
)


def test_has_and_get():
    cat = PluginCatalog(by_category={"indicators": {"ema": PluginManifest(
        name="ema", category="indicators", provenance="fwbg-core",
        version="1.0.0", source_path=Path("/tmp/x"),
    )}})
    assert cat.has("indicators", "ema")
    assert not cat.has("indicators", "nonexistent")
    assert not cat.has("models", "ema")
    assert cat.get("indicators", "ema").name == "ema"
    assert cat.get("indicators", "missing") is None


def test_all_slugs_for_returns_sorted():
    cat = PluginCatalog(by_category={"indicators": {
        slug: PluginManifest(name=slug, category="indicators", provenance="fwbg-core",
                             version="1.0.0", source_path=Path("/tmp/x"))
        for slug in ["sma", "ema", "macd"]
    }})
    assert cat.all_slugs_for("indicators") == ["ema", "macd", "sma"]
    assert cat.all_slugs_for("nonexistent") == []
