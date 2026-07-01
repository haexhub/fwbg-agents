"""Tests for the adaptive-runner universe planner (Phase 2)."""

from __future__ import annotations

from dataclasses import dataclass

from fwbg_agents.orchestrator.universe import (
    plan_universe_attempts,
    timeframes_by_symbol,
)


@dataclass
class _FakeStrategy:
    asset_class: str | None = None
    suggested_universe: list | None = None


def _labels(attempts):
    return [a.label for a in attempts]


def test_no_suggestion_no_class_is_single_unconstrained():
    attempts = plan_universe_attempts(_FakeStrategy())
    assert _labels(attempts) == ["unconstrained"]
    assert attempts[0].assets is None
    assert attempts[0].asset_classes is None


def test_no_suggestion_with_class_broadens_then_unconstrained():
    attempts = plan_universe_attempts(_FakeStrategy(asset_class="FOREX"))
    assert _labels(attempts) == ["class", "unconstrained"]
    assert attempts[0].asset_classes == ("FOREX",)
    assert attempts[0].assets is None


def test_suggested_symbols_ladder():
    s = _FakeStrategy(
        asset_class="FOREX",
        suggested_universe=[
            {"scope": "symbol", "value": "EURUSD", "timeframe": "HOUR_1", "rationale": "x"},
        ],
    )
    attempts = plan_universe_attempts(s)
    assert _labels(attempts) == ["suggested", "class", "unconstrained"]
    assert attempts[0].assets == ("EURUSD",)
    assert attempts[0].asset_classes is None
    assert attempts[1].asset_classes == ("FOREX",)


def test_suggested_symbols_and_classes():
    s = _FakeStrategy(
        asset_class="INDEX",
        suggested_universe=[
            {"scope": "symbol", "value": "EURUSD", "rationale": "x"},
            {"scope": "asset_class", "value": "FOREX", "rationale": "y"},
        ],
    )
    attempts = plan_universe_attempts(s)
    assert _labels(attempts) == ["suggested", "class", "unconstrained"]
    assert attempts[0].assets == ("EURUSD",)
    assert attempts[0].asset_classes == ("FOREX",)
    # class rung keeps the suggested class and adds the strategy's own class
    assert set(attempts[1].asset_classes) == {"FOREX", "INDEX"}
    assert attempts[1].assets is None


def test_class_only_suggestion_does_not_duplicate():
    # suggested class == strategy.asset_class -> "suggested" and "class" would
    # collapse; the dedupe keeps a single class rung.
    s = _FakeStrategy(
        asset_class="FOREX",
        suggested_universe=[{"scope": "asset_class", "value": "FOREX", "rationale": "x"}],
    )
    attempts = plan_universe_attempts(s)
    assert _labels(attempts) == ["suggested", "unconstrained"]
    assert attempts[0].asset_classes == ("FOREX",)


def test_symbols_deduplicated_and_ordered():
    s = _FakeStrategy(
        suggested_universe=[
            {"scope": "symbol", "value": "EURUSD", "rationale": "x"},
            {"scope": "symbol", "value": "GBPUSD", "rationale": "y"},
            {"scope": "symbol", "value": "EURUSD", "rationale": "z"},
        ],
    )
    attempts = plan_universe_attempts(s)
    assert attempts[0].assets == ("EURUSD", "GBPUSD")


def test_timeframes_by_symbol():
    s = _FakeStrategy(
        suggested_universe=[
            {"scope": "symbol", "value": "EURUSD", "timeframe": "HOUR_1", "rationale": "x"},
            {"scope": "symbol", "value": "GBPUSD", "rationale": "y"},  # no tf
            {"scope": "asset_class", "value": "FOREX", "timeframe": "DAY_1", "rationale": "z"},
        ],
    )
    assert timeframes_by_symbol(s) == {"EURUSD": "HOUR_1"}
