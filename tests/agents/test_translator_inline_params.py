"""Translator — inline pipeline param validation against plugin schemas."""

from __future__ import annotations

import pytest

from fwbg_agents.agents.translator import _validate_inline_params
from fwbg_agents.orchestrator.strategy_validator import StrategyValidationError


def _plugin_schema(name: str, param_schema: dict) -> dict:
    return {"name": name, "param_schema": param_schema}


def test_none_value_skips_choices_check():
    """A param left at its None default must not be checked against `choices`."""
    strategy = {
        "pipeline": {
            "indicators": [
                {"name": "time_season", "params": {"trading_days": None}},
            ]
        }
    }
    schemas = [
        _plugin_schema(
            "time_season",
            {"trading_days": {"choices": ["0", "1", "2", "3", "4", "5", "6"]}},
        )
    ]

    _validate_inline_params(strategy, schemas)  # must not raise


def test_invalid_value_still_rejected():
    strategy = {
        "pipeline": {
            "indicators": [
                {"name": "time_season", "params": {"trading_days": [7]}},
            ]
        }
    }
    schemas = [
        _plugin_schema(
            "time_season",
            {"trading_days": {"choices": ["0", "1", "2", "3", "4", "5", "6"]}},
        )
    ]

    with pytest.raises(StrategyValidationError, match="trading_days"):
        _validate_inline_params(strategy, schemas)


def test_valid_value_passes():
    strategy = {
        "pipeline": {
            "indicators": [
                {"name": "time_season", "params": {"trading_days": [0, 1, 2, 3, 4]}},
            ]
        }
    }
    schemas = [
        _plugin_schema(
            "time_season",
            {"trading_days": {"choices": ["0", "1", "2", "3", "4", "5", "6"]}},
        )
    ]

    _validate_inline_params(strategy, schemas)  # must not raise
