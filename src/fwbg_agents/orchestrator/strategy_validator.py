"""Lightweight structural validator for fwbg strategy.json (M4).

The Translator emits strategy.json; this validator catches obvious LLM
mistakes BEFORE we hand the file to fwbg. The full source of truth is
`fwbg.core.config.StrategyConfig` (pydantic), but pulling fwbg in as a
runtime dependency of this repo would be heavy — the Runner remains the
ultimate validator when fwbg starts the backtest.

The plugin-slug catalog is intentionally small in M4. M5's PluginAuthor
will expand it; Translator-fresh that needs a plugin not listed here must
keep the strategy in PROPOSED and emit a `needs_plugin: <slug>` note in
spec.md instead of fabricating a slug.
"""

from __future__ import annotations

from typing import Any

KNOWN_PIPELINES: frozenset[str] = frozenset({"orb_simple_v1"})
KNOWN_MODELS: frozenset[str] = frozenset({"signal_orb_v1"})
KNOWN_FILTERS: frozenset[str] = frozenset({"orb_scalping_v1"})
KNOWN_VALIDATIONS: frozenset[str] = frozenset({"walk_forward_intraday_v1"})
KNOWN_RESOURCES: frozenset[str] = frozenset({"standard_v1"})
KNOWN_DATASOURCES: frozenset[str] = frozenset({"forexsb"})
KNOWN_TIMEFRAMES: frozenset[str] = frozenset(
    {"MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR_1"}
)

REQUIRED_TOP_LEVEL: tuple[str, ...] = (
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
)

_FIELD_CATALOGS: tuple[tuple[str, frozenset[str]], ...] = (
    ("datasource", KNOWN_DATASOURCES),
    ("pipeline", KNOWN_PIPELINES),
    ("model", KNOWN_MODELS),
    ("filters", KNOWN_FILTERS),
    ("validation", KNOWN_VALIDATIONS),
    ("resources", KNOWN_RESOURCES),
    ("timeframe", KNOWN_TIMEFRAMES),
)


class StrategyValidationError(ValueError):
    """Raised when a strategy.json payload fails structural validation."""


def _check_exit_strategies(items: Any) -> None:
    if not isinstance(items, list) or not items:
        raise StrategyValidationError("exit_strategies must be a non-empty list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise StrategyValidationError(f"exit_strategies[{i}] must be an object")
        if "name" not in item or not isinstance(item["name"], str):
            raise StrategyValidationError(f"exit_strategies[{i}].name is required (str)")
        if "params" not in item or not isinstance(item["params"], dict):
            raise StrategyValidationError(f"exit_strategies[{i}].params is required (object)")


def _check_tags(tags: Any) -> None:
    if not isinstance(tags, list) or not tags:
        raise StrategyValidationError("tags must be a non-empty list")
    for t in tags:
        if not isinstance(t, str):
            raise StrategyValidationError("tags entries must be strings")


def validate_strategy_json(data: dict) -> None:
    """Raise StrategyValidationError if `data` is not a structurally valid strategy.json."""
    if not isinstance(data, dict):
        raise StrategyValidationError("payload must be a JSON object")

    missing = [k for k in REQUIRED_TOP_LEVEL if k not in data]
    if missing:
        raise StrategyValidationError(f"missing required keys: {missing}")

    for field, catalog in _FIELD_CATALOGS:
        value = data[field]
        if not isinstance(value, str):
            raise StrategyValidationError(f"{field} must be a string")
        if value not in catalog:
            raise StrategyValidationError(
                f"{field}={value!r} is not in the known catalog "
                f"({sorted(catalog)}). The Translator must keep the strategy in "
                "PROPOSED and emit a 'needs_plugin' note instead of inventing slugs."
            )

    _check_exit_strategies(data["exit_strategies"])
    _check_tags(data["tags"])

    if not isinstance(data["name"], str) or not data["name"]:
        raise StrategyValidationError("name must be a non-empty string")
    if not isinstance(data["hypothesis"], str) or not data["hypothesis"]:
        raise StrategyValidationError("hypothesis must be a non-empty string")
