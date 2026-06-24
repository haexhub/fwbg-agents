"""Tests for orchestrator.plugin_catalog — fwbg manifest discovery + DB merge.

The catalog replaces the M4 hard-coded KNOWN_* frozensets. Discovery scans
two roots:
  - `<fwbg_root>/src/fwbg/plugins/<bundle>/manifest.json` (fwbg-core)
  - `<fwbg_root>/packages/fwbg-premium/src/fwbg_premium/plugins/<bundle>/manifest.json`

Both layouts use the same BUNDLE shape: top-level `plugins: {category: [slug, ...]}`.

DB-side plugins are merged in only when current_state IN (verified, adopted_in_fwbg)
— authored-but-unverified plugins MUST NOT pass strategy validation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
    _load_fwbg_cached,
    discover_fwbg_plugins,
    load_catalog,
    merge_with_db,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Plugin, PluginState


def _write_bundle(root: Path, rel_dir: str, name: str, plugins: dict) -> Path:
    """Write a fwbg-style bundle manifest at <root>/<rel_dir>/<name>/manifest.json."""
    d = root / rel_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "plugins": plugins})
    )
    return d / "manifest.json"


@pytest.fixture(autouse=True)
def _clear_cache():
    _load_fwbg_cached.cache_clear()
    yield
    _load_fwbg_cached.cache_clear()


@pytest.fixture
def fwbg_root(tmp_path: Path) -> Path:
    """A faked fwbg repo root with core + premium bundles."""
    _write_bundle(
        tmp_path,
        "src/fwbg/plugins",
        "fwbg-core",
        {"indicators": ["ema", "sma"], "exit_strategies": ["fixed"]},
    )
    _write_bundle(
        tmp_path,
        "packages/fwbg-premium/src/fwbg_premium/plugins",
        "fwbg-premium",
        {
            "indicators": ["regime"],
            "feature_selection": ["boruta"],
            "exit_strategies": ["atr_based"],
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# discover_fwbg_plugins
# ---------------------------------------------------------------------------


def test_discover_fwbg_core_indicators(fwbg_root):
    cat = discover_fwbg_plugins(fwbg_root)
    assert "ema" in cat["indicators"]
    assert cat["indicators"]["ema"].provenance == "fwbg-core"
    assert cat["indicators"]["ema"].category == "indicators"


def test_discover_premium_feature_selection(fwbg_root):
    cat = discover_fwbg_plugins(fwbg_root)
    assert "boruta" in cat["feature_selection"]
    assert cat["feature_selection"]["boruta"].provenance == "fwbg-premium"


def test_discover_missing_root_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    cat = discover_fwbg_plugins(missing)
    assert cat == {}


def test_discover_malformed_json_skipped(tmp_path, caplog):
    d = tmp_path / "src" / "fwbg" / "plugins" / "broken"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not valid json")
    cat = discover_fwbg_plugins(tmp_path)
    assert cat == {}
    assert any("broken" in r.message or "manifest" in r.message.lower() for r in caplog.records)


def test_discover_premium_overlaps_core_keeps_both(fwbg_root):
    """Same category, different slugs → both present in the same category bucket."""
    cat = discover_fwbg_plugins(fwbg_root)
    assert "fixed" in cat["exit_strategies"]
    assert "atr_based" in cat["exit_strategies"]
    assert cat["exit_strategies"]["fixed"].provenance == "fwbg-core"
    assert cat["exit_strategies"]["atr_based"].provenance == "fwbg-premium"


# ---------------------------------------------------------------------------
# merge_with_db
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/catalog.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


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


# ---------------------------------------------------------------------------
# load_catalog + caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_catalog_includes_fwbg_and_db(fwbg_root, db_session, monkeypatch):
    from fwbg_agents.config import settings as _settings

    monkeypatch.setattr(_settings, "fwbg_repo_root", fwbg_root)

    db_session.add(_make_plugin("zone_pivots", "indicators", PluginState.VERIFIED))
    await db_session.commit()

    cat = await load_catalog(db_session)
    assert cat.has("indicators", "ema")          # fwbg-core
    assert cat.has("indicators", "regime")       # fwbg-premium
    assert cat.has("indicators", "zone_pivots")  # agent-authored


@pytest.mark.asyncio
async def test_load_catalog_caches_fwbg_scan(fwbg_root, db_session, monkeypatch):
    """Second call with same fwbg_root must not re-read disk."""
    from fwbg_agents.config import settings as _settings

    monkeypatch.setattr(_settings, "fwbg_repo_root", fwbg_root)

    await load_catalog(db_session)
    # Move the manifest file out — if caching works, the second load still sees it.
    (fwbg_root / "src" / "fwbg" / "plugins" / "fwbg-core" / "manifest.json").unlink()
    cat2 = await load_catalog(db_session)
    assert cat2.has("indicators", "ema"), "cache should preserve discovery across calls"

    _load_fwbg_cached.cache_clear()
    cat3 = await load_catalog(db_session)
    assert not cat3.has("indicators", "ema"), "cache_clear should re-scan disk"
