"""Lightweight strategy.json structural validator (M4 → M5a catalog-aware)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog, PluginManifest
from fwbg_agents.orchestrator.strategy_validator import (
    KNOWN_FILTERS,
    KNOWN_MODELS,
    KNOWN_PIPELINES,
    KNOWN_RESOURCES,
    KNOWN_VALIDATIONS,
    StrategyValidationError,
    validate_strategy_json,
)


def _manifest(slug: str, category: str) -> PluginManifest:
    return PluginManifest(
        name=slug, category=category, provenance="fwbg-core",
        version="1.0.0", source_path=Path("/tmp/x"),
    )


def _catalog(by_category: dict[str, list[str]]) -> PluginCatalog:
    return PluginCatalog(by_category={
        cat: {s: _manifest(s, cat) for s in slugs}
        for cat, slugs in by_category.items()
    })


VALID_FIXTURE = {
    "name": "test_strategy",
    "datasource": "forexsb",
    "description": "x",
    "hypothesis": "y",
    "expected_outcome": "z",
    "pipeline": "orb_simple_v1",
    "model": "signal_orb_v1",
    "filters": "orb_scalping_v1",
    "validation": "walk_forward_intraday_v1",
    "resources": "standard_v1",
    "timeframe": "MINUTE_15",
    "exit_strategies": [
        {
            "name": "orb_based",
            "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14},
            "ct": [0.5],
        }
    ],
    "tags": ["orb", "intraday"],
    "optimization": {},
}


def test_valid_payload_passes():
    validate_strategy_json(VALID_FIXTURE)


def test_real_m3_smoke_fixture_passes():
    """Sanity: the real fixture written by the M3 smoke loads under M4's validator."""
    fixture = Path(__file__).resolve().parents[2] / "data" / "strategies"
    # If the smoke artifacts have been gc'd in CI, skip silently — this test
    # only asserts compatibility when the artifact happens to be present.
    candidates = list(fixture.glob("m3_smoke_*/iteration_001/strategy.json"))
    if not candidates:
        pytest.skip("no M3 smoke artifact present")
    data = json.loads(candidates[0].read_text())
    validate_strategy_json(data)


@pytest.mark.parametrize(
    "missing_key",
    [
        "name",
        "datasource",
        "pipeline",
        "model",
        "filters",
        "validation",
        "resources",
        "timeframe",
        "exit_strategies",
        "tags",
        "hypothesis",
    ],
)
def test_missing_required_top_level_key_fails(missing_key):
    payload = dict(VALID_FIXTURE)
    payload.pop(missing_key)
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload)
    assert missing_key in str(exc.value)


def test_unknown_pipeline_slug_fails():
    payload = dict(VALID_FIXTURE)
    payload["pipeline"] = "made_up_pipeline_v99"
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload)
    assert "pipeline" in str(exc.value)


def test_unknown_model_slug_fails():
    payload = dict(VALID_FIXTURE)
    payload["model"] = "phantom_model"
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload)


def test_unknown_timeframe_fails_against_live_list():
    payload = dict(VALID_FIXTURE)
    payload["timeframe"] = "MINUTE_3"
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload, timeframes=["MINUTE_15", "HOUR_1", "DAY_1"])


def test_daily_timeframe_is_allowed():
    """The old frozen intraday set silently forbade DAY_1 although fwbg
    supports it — daily strategies must be expressible."""
    payload = dict(VALID_FIXTURE)
    payload["timeframe"] = "DAY_1"
    validate_strategy_json(payload)  # offline: lax
    validate_strategy_json(payload, timeframes=["MINUTE_15", "HOUR_1", "DAY_1"])


def test_empty_exit_strategies_fails():
    payload = dict(VALID_FIXTURE)
    payload["exit_strategies"] = []
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload)


def test_exit_strategy_missing_name_fails():
    payload = dict(VALID_FIXTURE)
    payload["exit_strategies"] = [{"params": {}}]
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload)


def test_exit_strategy_missing_params_fails():
    payload = dict(VALID_FIXTURE)
    payload["exit_strategies"] = [{"name": "orb_based"}]
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload)


def test_empty_tags_fails():
    payload = dict(VALID_FIXTURE)
    payload["tags"] = []
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload)


def test_known_constants_are_non_empty():
    """Sanity that the catalog wasn't accidentally emptied."""
    assert KNOWN_PIPELINES
    assert KNOWN_MODELS
    assert KNOWN_FILTERS
    assert KNOWN_VALIDATIONS
    assert KNOWN_RESOURCES


# ---------------------------------------------------------------------------
# M5a: catalog injection
# ---------------------------------------------------------------------------


def test_legacy_no_catalog_call_still_passes():
    """Calling with catalog=None must behave exactly like M4."""
    validate_strategy_json(VALID_FIXTURE)
    validate_strategy_json(VALID_FIXTURE, catalog=None)


def test_catalog_extends_known_models():
    """A model not in the legacy frozenset but present in the catalog must pass."""
    payload = dict(VALID_FIXTURE)
    payload["model"] = "brand_new_model_v2"
    cat = _catalog({"models": ["brand_new_model_v2"]})
    validate_strategy_json(payload, catalog=cat)


def test_catalog_rejects_unknown_model_with_suggestion():
    payload = dict(VALID_FIXTURE)
    payload["model"] = "signal_orb_typo"
    cat = _catalog({"models": ["signal_orb_v1", "signal_orb_v2"]})
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload, catalog=cat)
    msg = str(exc.value)
    assert "signal_orb_typo" in msg
    assert "signal_orb_v1" in msg  # suggestion


def test_catalog_accepts_exit_strategies_name():
    payload = dict(VALID_FIXTURE)
    payload["exit_strategies"] = [{"name": "atr_based", "params": {"atr_period": 14}}]
    cat = _catalog({"exit_strategies": ["fixed", "atr_based"]})
    validate_strategy_json(payload, catalog=cat)


def test_catalog_rejects_unknown_exit_strategies_name():
    payload = dict(VALID_FIXTURE)
    payload["exit_strategies"] = [{"name": "phantom_exit", "params": {}}]
    cat = _catalog({"exit_strategies": ["fixed", "atr_based"]})
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload, catalog=cat)
    assert "phantom_exit" in str(exc.value)


def test_catalog_empty_falls_back_to_frozenset():
    """Catalog with no entries for a category → frozenset still rules that category."""
    cat = _catalog({"indicators": ["ema"]})  # no "models", no "exit_strategies"
    # `signal_orb_v1` is in the frozenset, so validation must pass even with
    # an empty `models` catalog category.
    validate_strategy_json(VALID_FIXTURE, catalog=cat)


# ---------------------------------------------------------------------------
# M5c: plugin-slot list-fields (indicators / feature_selection /
# preprocessing / extra_filters)
# ---------------------------------------------------------------------------


def test_strategy_with_no_list_fields_is_valid():
    """Regression guard: M4-shape strategy.json (no list-fields) still validates."""
    validate_strategy_json(VALID_FIXTURE)
    # Same payload with explicit empty lists must also pass.
    payload = dict(VALID_FIXTURE)
    payload["indicators"] = []
    payload["feature_selection"] = []
    payload["preprocessing"] = []
    payload["extra_filters"] = []
    validate_strategy_json(payload)


def test_indicators_list_must_be_list_of_str():
    # String instead of list
    payload = dict(VALID_FIXTURE)
    payload["indicators"] = "adx"
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload)
    assert "indicators" in str(exc.value)

    # Dict inside list
    payload = dict(VALID_FIXTURE)
    payload["indicators"] = [{"x": 1}]
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload)
    assert "indicators" in str(exc.value)

    # None inside list
    payload = dict(VALID_FIXTURE)
    payload["indicators"] = [None]
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload)
    assert "indicators" in str(exc.value)


def test_indicators_empty_list_is_valid():
    payload = dict(VALID_FIXTURE)
    payload["indicators"] = []
    validate_strategy_json(payload)


def test_indicators_slug_must_be_in_catalog_when_catalog_present():
    cat = _catalog({"indicators": ["adx-trend-strength", "ema-cross"]})

    payload = dict(VALID_FIXTURE)
    payload["indicators"] = ["adx-trend-strength"]
    validate_strategy_json(payload, catalog=cat)

    # Unknown slug → error mentioning the offending slug.
    payload_bad = dict(VALID_FIXTURE)
    payload_bad["indicators"] = ["made-up"]
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload_bad, catalog=cat)
    assert "made-up" in str(exc.value)

    # Close typo → did-you-mean hint surfaces the real slug.
    payload_typo = dict(VALID_FIXTURE)
    payload_typo["indicators"] = ["adx-trend-strenght"]  # typo
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload_typo, catalog=cat)
    msg = str(exc.value)
    assert "adx-trend-strenght" in msg
    assert "adx-trend-strength" in msg  # did-you-mean


def test_extra_filters_routes_to_catalog_filters_category():
    # Catalog has the slug only under "filters" category; the
    # `extra_filters` field must route to "filters" (not "extra_filters").
    cat = _catalog({"filters": ["custom-filter-x"]})

    payload = dict(VALID_FIXTURE)
    payload["extra_filters"] = ["custom-filter-x"]
    validate_strategy_json(payload, catalog=cat)

    # An unknown slug against the "filters" category must be rejected and
    # the error must mention the "filters" category — proving routing.
    payload_bad = dict(VALID_FIXTURE)
    payload_bad["extra_filters"] = ["custom-filter-y"]
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy_json(payload_bad, catalog=cat)
    msg = str(exc.value)
    assert "custom-filter-y" in msg
    assert "filters" in msg
    # category was 'filters', not 'extra_filters'
    assert "extra_filters" not in msg.split("category")[1]


def test_no_catalog_means_lax_membership_for_list_fields():
    """Without catalog kwarg, arbitrary slugs in list-fields pass (M4-compat)."""
    payload = dict(VALID_FIXTURE)
    payload["indicators"] = ["anything-goes"]
    payload["feature_selection"] = ["another"]
    payload["preprocessing"] = ["whatever"]
    payload["extra_filters"] = ["nope"]
    validate_strategy_json(payload)


# ──────────────────────────────────────────────
# M7: inline composition (pipeline/model/filters as dicts) + live presets
# ──────────────────────────────────────────────

INLINE_FIXTURE = {
    **VALID_FIXTURE,
    "pipeline": {
        "indicators": [{"name": "opening_range", "params": {"range_bars": [1, 2]}}],
        "preprocessing": [{"name": "fractional_diff", "params": {}}],
    },
    "model": {
        "type": "xgboost",
        "architecture": "unified",
        "trade_directions": ["long", "short"],
        "hyperparameters": {"max_depth": 3},
    },
    "filters": {"min_trades": 50, "min_sharpe": 0.5},
}

_INLINE_CATALOG = _catalog({
    "indicators": ["opening_range", "atr"],
    "preprocessing": ["fractional_diff"],
    "models": ["xgboost", "signal"],
    "exit_strategies": ["orb_based"],
})


def test_inline_composition_passes_with_catalog():
    validate_strategy_json(dict(INLINE_FIXTURE), catalog=_INLINE_CATALOG)


def test_inline_pipeline_requires_indicators():
    bad = {**INLINE_FIXTURE, "pipeline": {"preprocessing": []}}
    with pytest.raises(StrategyValidationError, match="indicators"):
        validate_strategy_json(bad, catalog=_INLINE_CATALOG)


def test_inline_pipeline_rejects_unknown_phase():
    bad = {**INLINE_FIXTURE, "pipeline": {**INLINE_FIXTURE["pipeline"], "exits": []}}
    with pytest.raises(StrategyValidationError, match="unknown phase"):
        validate_strategy_json(bad, catalog=_INLINE_CATALOG)


def test_inline_pipeline_rejects_unknown_plugin_name():
    bad = {
        **INLINE_FIXTURE,
        "pipeline": {"indicators": [{"name": "made_up_indicator", "params": {}}]},
    }
    with pytest.raises(StrategyValidationError, match="made_up_indicator"):
        validate_strategy_json(bad, catalog=_INLINE_CATALOG)


def test_inline_pipeline_names_lax_without_catalog():
    # No catalog → shape-checked only (offline fallback keeps research working).
    validate_strategy_json(dict(INLINE_FIXTURE), catalog=None)


def test_inline_model_rejects_unknown_type():
    bad = {**INLINE_FIXTURE, "model": {"type": "phantom_model"}}
    with pytest.raises(StrategyValidationError, match="phantom_model"):
        validate_strategy_json(bad, catalog=_INLINE_CATALOG)


def test_inline_model_rejects_bad_architecture():
    bad = {**INLINE_FIXTURE, "model": {"type": "xgboost", "architecture": "tri_leg"}}
    with pytest.raises(StrategyValidationError, match="architecture"):
        validate_strategy_json(bad, catalog=_INLINE_CATALOG)


def test_inline_model_rejects_bad_trade_directions():
    bad = {**INLINE_FIXTURE, "model": {"type": "xgboost", "trade_directions": ["up"]}}
    with pytest.raises(StrategyValidationError, match="trade_directions"):
        validate_strategy_json(bad, catalog=_INLINE_CATALOG)


def test_validation_preset_checked_against_live_presets():
    presets = {"validations": ["walk_forward_exploration_v1"], "resources": ["standard_v1"]}
    good = {**INLINE_FIXTURE, "validation": "walk_forward_exploration_v1"}
    validate_strategy_json(good, catalog=_INLINE_CATALOG, presets=presets)
    with pytest.raises(StrategyValidationError, match="validation"):
        # walk_forward_intraday_v1 is NOT in the live preset list above.
        validate_strategy_json(dict(INLINE_FIXTURE), catalog=_INLINE_CATALOG, presets=presets)


def test_legacy_pipeline_string_checked_against_live_presets():
    presets = {"pipelines": ["my_custom_pipeline_v2"]}
    good = {**VALID_FIXTURE, "pipeline": "my_custom_pipeline_v2"}
    validate_strategy_json(good, presets=presets)
    with pytest.raises(StrategyValidationError, match="pipeline"):
        validate_strategy_json(dict(VALID_FIXTURE), presets=presets)


def test_datasource_checked_against_live_configured_sources():
    """The frozen 'forexsb' default caused instantly-failing runs on machines
    where only e.g. 'eur-usd' is configured — the live list must win."""
    good = {**INLINE_FIXTURE, "datasource": "eur-usd"}
    validate_strategy_json(good, catalog=_INLINE_CATALOG, datasources=["eur-usd"])
    with pytest.raises(StrategyValidationError, match="datasource"):
        # 'forexsb' is not configured on this deployment.
        validate_strategy_json(
            dict(INLINE_FIXTURE), catalog=_INLINE_CATALOG, datasources=["eur-usd"]
        )


def test_datasource_unchecked_without_live_list():
    """No hardcoded datasource fallback exists anymore: offline (no live
    list) the ref passes unchecked — the Runner is the ultimate validator."""
    any_name = {**INLINE_FIXTURE, "datasource": "whatever-source"}
    validate_strategy_json(any_name, catalog=_INLINE_CATALOG, datasources=None)
