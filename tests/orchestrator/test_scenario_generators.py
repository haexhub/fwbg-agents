"""Deterministic OHLCV scenario generators — pure-function unit tests.

Locked decision (M5a): hand-curated np-seeded generators only. No data-derived
thresholds. Same seed must produce the same frame across runs and across
machines (CPython + numpy float64 are deterministic for this).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fwbg_agents.orchestrator.scenario_generators import (
    SCENARIO_GENERATORS,
    generate_scenario,
)


EXPECTED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


def test_registry_lists_all_five_scenarios():
    assert set(SCENARIO_GENERATORS) == {
        "trending_up",
        "trending_down",
        "sideways",
        "high_vola",
        "sparse_data",
    }


@pytest.mark.parametrize("name", sorted(SCENARIO_GENERATORS))
def test_each_generator_returns_frame_with_expected_columns(name):
    df = generate_scenario(name)
    assert isinstance(df, pd.DataFrame)
    assert set(df.columns) == EXPECTED_COLUMNS
    assert len(df) > 0
    assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])
    assert pd.api.types.is_numeric_dtype(df["close"])


@pytest.mark.parametrize("name", sorted(SCENARIO_GENERATORS))
def test_each_generator_is_deterministic(name):
    a = generate_scenario(name)
    b = generate_scenario(name)
    pd.testing.assert_frame_equal(a, b)


def test_trending_up_close_is_monotonically_rising_on_average():
    df = generate_scenario("trending_up")
    # Compare first vs last decile means — drift must be positive.
    first = df["close"].iloc[: len(df) // 10].mean()
    last = df["close"].iloc[-len(df) // 10 :].mean()
    assert last > first


def test_trending_down_close_falls_on_average():
    df = generate_scenario("trending_down")
    first = df["close"].iloc[: len(df) // 10].mean()
    last = df["close"].iloc[-len(df) // 10 :].mean()
    assert last < first


def test_high_vola_has_larger_close_std_than_sideways():
    sideways = generate_scenario("sideways")["close"].std()
    hivola = generate_scenario("high_vola")["close"].std()
    assert hivola > sideways * 1.5


def test_sparse_data_has_gaps():
    df = generate_scenario("sparse_data")
    diffs = df["timestamp"].diff().dropna().dt.total_seconds()
    # Default is 1-minute spacing (60s). Sparse data must have at least 5
    # gaps wider than that.
    assert (diffs > 60).sum() >= 5


def test_ohlc_invariants_hold_on_every_generator():
    for name in SCENARIO_GENERATORS:
        df = generate_scenario(name)
        assert (df["high"] >= df["low"]).all(), f"{name}: high < low"
        assert (df["high"] >= df["open"]).all(), f"{name}: high < open"
        assert (df["high"] >= df["close"]).all(), f"{name}: high < close"
        assert (df["low"] <= df["open"]).all(), f"{name}: low > open"
        assert (df["low"] <= df["close"]).all(), f"{name}: low > close"
        assert (df["volume"] >= 0).all(), f"{name}: negative volume"


def test_unknown_scenario_raises_key_error():
    with pytest.raises(KeyError):
        generate_scenario("does_not_exist")
