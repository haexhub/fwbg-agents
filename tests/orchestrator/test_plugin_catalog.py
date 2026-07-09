"""Tests for orchestrator.plugin_catalog — catalog types + DB merge.

Plugin discovery is API-only now (see live_catalog / test_live_catalog); this
module covers the shared catalog types and `merge_with_db`, which layers
agent-authored DB plugins onto the API-built catalog.

DB-side plugins are merged in only when current_state IN (verified, adopted_in_fwbg)
— authored-but-unverified plugins MUST NOT pass strategy validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
    merge_with_db,
)
from fwbg_agents.persistence.models import Plugin, PluginState


def _make_plugin(slug: str, kind: str, state: PluginState) -> Plugin:
    now = datetime.now(UTC)
    return Plugin(
        slug=slug,
        current_state=state.value,
        kind=kind,
        spec_path=f"data/plugins/{slug}/v1/spec.md",
        contract_path=f"data/plugins/{slug}/v1/contract.yaml",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# merge_with_db
# ---------------------------------------------------------------------------


def test_merge_skips_specified_and_authored():
    fwbg_only = {"indicators": {"ema": PluginManifest(
        name="ema", category="indicators", provenance="fwbg-core",
        version="1.0.0", source_path=Path("/tmp/x"),
    )}}
    db_plugins = [
        _make_plugin("under_dev", "indicators", PluginState.SPECIFIED),
        _make_plugin("nearly_done", "indicators", PluginState.AUTHORED),
    ]
    merged = merge_with_db(fwbg_only, db_plugins)
    assert "under_dev" not in merged.by_category.get("indicators", {})
    assert "nearly_done" not in merged.by_category.get("indicators", {})


def test_merge_includes_verified():
    db_plugins = [_make_plugin("zone_pivots", "indicators", PluginState.VERIFIED)]
    merged = merge_with_db({}, db_plugins)
    assert "zone_pivots" in merged.by_category["indicators"]
    assert merged.by_category["indicators"]["zone_pivots"].provenance == "agent-authored"


def test_db_shadows_fwbg_same_slug():
    """Agent-authored plugin with same slug as fwbg-side: agent wins (post-promote semantics)."""
    fwbg_cat = {"indicators": {"ema": PluginManifest(
        name="ema", category="indicators", provenance="fwbg-core",
        version="1.0.0", source_path=Path("/tmp/x"),
    )}}
    db_plugins = [_make_plugin("ema", "indicators", PluginState.ADOPTED_IN_FWBG)]
    merged = merge_with_db(fwbg_cat, db_plugins)
    assert merged.by_category["indicators"]["ema"].provenance == "agent-authored"


def test_merge_maps_singular_kind_to_plural_category():
    """PluginAuthor writes Plugin.kind=PluginContract.PluginKindLit (singular).

    The validator queries the plural bundle-manifest category. The merge must
    remap so DB-VERIFIED plugins land in the bucket the validator queries.
    """
    db_plugins = [_make_plugin("rsi_v2", "indicator", PluginState.VERIFIED)]
    merged = merge_with_db({}, db_plugins)
    assert "rsi_v2" in merged.by_category["indicators"]
    assert merged.by_category["indicators"]["rsi_v2"].category == "indicators"
    assert "rsi_v2" not in merged.by_category.get("indicator", {})


def test_merge_maps_filter_kind_to_filters_category():
    """`filter` and `risk_management` DB kinds both land in the `filters` bucket
    the validator queries (fwbg has no distinct `filters` phase)."""
    db_plugins = [
        _make_plugin("regime_gate", "filter", PluginState.VERIFIED),
        _make_plugin("kelly_v2", "risk_management", PluginState.VERIFIED),
    ]
    merged = merge_with_db({}, db_plugins)
    assert "regime_gate" in merged.by_category["filters"]
    assert "kelly_v2" in merged.by_category["filters"]


def test_merge_handles_multiword_kinds_unchanged():
    """Multi-word categories like feature_selection don't pluralize — map to themselves."""
    db_plugins = [_make_plugin("boruta_v2", "feature_selection", PluginState.VERIFIED)]
    merged = merge_with_db({}, db_plugins)
    assert "boruta_v2" in merged.by_category["feature_selection"]
    assert merged.by_category["feature_selection"]["boruta_v2"].category == "feature_selection"


def test_merge_unknown_kind_passes_through():
    """Kinds with no _KIND_TO_CATEGORY entry fall back to the verbatim string."""
    db_plugins = [_make_plugin("custom", "custom_unknown_kind", PluginState.VERIFIED)]
    merged = merge_with_db({}, db_plugins)
    assert "custom" in merged.by_category["custom_unknown_kind"]


# ---------------------------------------------------------------------------
# PluginCatalog helpers
# ---------------------------------------------------------------------------


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
