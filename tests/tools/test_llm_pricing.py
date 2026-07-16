"""Unit tests for the LLM list-price estimator (Plan 018)."""

from __future__ import annotations

import pytest

from fwbg_agents.config import settings
from fwbg_agents.tools.llm_pricing import estimate_cost_usd


def test_known_model_exact_match():
    # claude-opus-4-7: $5/1M input, $25/1M output
    cost = estimate_cost_usd("claude-opus-4-7", 1_000_000, 1_000_000)
    assert cost == pytest.approx(30.0)


def test_known_model_date_suffixed_match():
    cost = estimate_cost_usd("claude-opus-4-8-20260115", 2_000_000, 0)
    assert cost == pytest.approx(10.0)


def test_unknown_model_returns_none():
    assert estimate_cost_usd("gpt-oss-120b", 1000, 1000) is None
    assert estimate_cost_usd("tavily-search", 0, 0) is None


def test_zero_tokens_known_model_is_zero_not_none():
    assert estimate_cost_usd("claude-opus-4-7", 0, 0) == 0.0


def test_settings_json_override_and_longest_match(monkeypatch):
    monkeypatch.setattr(
        settings,
        "llm_price_table_json",
        '{"claude-x": [1.0, 2.0], "claude-x-large": [10.0, 20.0]}',
    )
    # Longest matching key wins for the more specific model string.
    assert estimate_cost_usd("claude-x-large-20260101", 1_000_000, 0) == pytest.approx(10.0)
    assert estimate_cost_usd("claude-x-20260101", 1_000_000, 0) == pytest.approx(1.0)
    # Override replaces the default table entirely.
    assert estimate_cost_usd("claude-opus-4-7", 1000, 1000) is None


def test_malformed_settings_json_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(settings, "llm_price_table_json", "not json {")
    cost = estimate_cost_usd("claude-opus-4-7", 1_000_000, 0)
    assert cost == pytest.approx(5.0)


def test_explicit_empty_override_leaves_all_models_unpriced(monkeypatch):
    monkeypatch.setattr(settings, "llm_price_table_json", "{}")
    assert estimate_cost_usd("claude-opus-4-7", 1_000_000, 1_000_000) is None
