"""Lightweight strategy.json structural validator (M4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fwbg_agents.orchestrator.strategy_validator import (
    KNOWN_DATASOURCES,
    KNOWN_FILTERS,
    KNOWN_MODELS,
    KNOWN_PIPELINES,
    KNOWN_RESOURCES,
    KNOWN_TIMEFRAMES,
    KNOWN_VALIDATIONS,
    StrategyValidationError,
    validate_strategy_json,
)


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


def test_unknown_timeframe_fails():
    payload = dict(VALID_FIXTURE)
    payload["timeframe"] = "MINUTE_3"
    with pytest.raises(StrategyValidationError):
        validate_strategy_json(payload)


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
    assert KNOWN_DATASOURCES
    assert KNOWN_TIMEFRAMES
