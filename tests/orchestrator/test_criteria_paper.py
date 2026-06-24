"""Tests for paper-criteria loader + evaluator (M6b Task 3)."""

from __future__ import annotations

import pytest

from fwbg_agents.orchestrator.criteria_paper import (
    CriteriaEvalResult,
    evaluate_paper_criteria,
    load_paper_criteria,
)
from fwbg_agents.tools.fwbg_paper_reader import PaperTradeSummary


def _make_summary(**overrides):
    base = dict(
        strategy_slug="test-strategy",
        sharpe_paper=1.2,
        max_dd_paper=0.10,
        trades_total=50,
        trades_today=2,
        days_in_paper=45,
        win_rate=0.55,
        last_trade_at="2026-06-20T10:00:00Z",
        current_equity=10500.0,
        starting_equity=10000.0,
        equity_curve_sample=[],
    )
    base.update(overrides)
    return PaperTradeSummary(**base)


def test_load_paper_criteria_forex_returns_dict_with_required_keys():
    d = load_paper_criteria("forex")
    assert "required_all" in d
    assert "hard_blockers" in d


def test_load_paper_criteria_unknown_class_raises():
    with pytest.raises(FileNotFoundError):
        load_paper_criteria("nonexistent")


def test_evaluate_passes_when_all_metrics_clear_thresholds():
    criteria = {
        "required_all": [{"sharpe_paper": ">= 0.8"}],
        "hard_blockers": [{"max_dd_paper": "<= 0.25"}],
    }
    res = evaluate_paper_criteria(
        _make_summary(sharpe_paper=1.0, max_dd_paper=0.10), criteria
    )
    assert isinstance(res, CriteriaEvalResult)
    assert res.passed is True
    assert res.failures == []


def test_evaluate_fails_when_hard_blocker_breached():
    criteria = {
        "required_all": [{"sharpe_paper": ">= 0.8"}],
        "hard_blockers": [{"max_dd_paper": "<= 0.25"}],
    }
    res = evaluate_paper_criteria(
        _make_summary(sharpe_paper=1.0, max_dd_paper=0.30), criteria
    )
    assert res.passed is False
    assert any("max_dd_paper" in f for f in res.failures)
