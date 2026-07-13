"""StrategySpec tests (Plan 009 WP5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fwbg_agents.orchestrator.hypotheses import (
    ResearcherHypothesis,
    Source,
    SuggestedUniverse,
    strategy_spec_from_hypothesis,
)
from fwbg_agents.speckit.strategy_spec import (
    StrategySpec,
    render_strategy_spec_md,
)


def test_edge_mechanism_must_be_single_line():
    with pytest.raises(ValidationError):
        StrategySpec(strategy_family="ORB", edge_mechanism="line one\nline two here")


def test_render_contains_edge_and_family():
    spec = StrategySpec(
        strategy_family="mean_reversion",
        edge_mechanism="RSI extremes mean-revert after London-open overreactions",
        timeframe="MINUTE_15",
        universe=["EURUSD", "GBPUSD"],
    )
    md = render_strategy_spec_md(spec)
    assert "mean_reversion" in md
    assert "RSI extremes mean-revert" in md
    assert "MINUTE_15" in md
    assert "EURUSD" in md


def test_from_hypothesis_maps_fields_and_universe():
    hyp = ResearcherHypothesis(
        title="t",
        asset_class="FOREX",
        strategy_family="liquidity_sweep",
        edge_mechanism="stops above prior-day high get swept then price reverts",
        hypothesis="h",
        expected_edge_explanation="e",
        entry_logic="enter on sweep + reversal candle",
        exit_mechanism="fixed TP at prior close",
        regime_assumption="range-bound sessions",
        filters=["adx < 20"],
        key_indicators=["prev_day_high"],
        tags=["liquidity"],
        sources=[Source(url="https://x", title="x", why_relevant="x")],
        suggested_universe=[
            SuggestedUniverse(scope="symbol", value="EURUSD", timeframe="MINUTE_15", rationale="x"),
            SuggestedUniverse(scope="symbol", value="GBPUSD", rationale="x"),
            SuggestedUniverse(scope="symbol", value="USDJPY", rationale="x"),
        ],
    )
    spec = strategy_spec_from_hypothesis(hyp)
    assert spec.strategy_family == "liquidity_sweep"
    assert spec.edge_mechanism.startswith("stops above")
    assert spec.entry_logic == "enter on sweep + reversal candle"
    assert spec.filters == ["adx < 20"]
    assert spec.timeframe == "MINUTE_15"  # first universe entry with a timeframe
    assert spec.universe == ["EURUSD", "GBPUSD", "USDJPY"]


def test_hypothesis_rejects_family_outside_vocabulary():
    with pytest.raises(ValidationError):
        ResearcherHypothesis(
            title="t",
            asset_class="FOREX",
            strategy_family="totally_new_family",  # not in the controlled vocab
            edge_mechanism="some mechanism that is long enough",
            hypothesis="h",
            expected_edge_explanation="e",
            key_indicators=["x"],
            tags=["y"],
            sources=[Source(url="https://x", title="x", why_relevant="x")],
        )
