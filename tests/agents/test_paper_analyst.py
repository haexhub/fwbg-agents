"""PaperAnalyst agent tests (M6b Task 4).

The PaperAnalyst is a pydantic-ai agent that ingests paper-trading telemetry
and emits one of three decisions:

  - PromotePaperToLive
  - AbandonPaper (validator fills default post_mortem_path when None)
  - ContinueObservation (validator forces stale=True when overdue)

We mock the LLM with pydantic-ai's `FunctionModel` (matching M3's pattern in
`tests/agents/test_analyst.py`), emitting `final_result_<Variant>` tool
calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fwbg_agents.agents.paper_analyst import (
    AbandonPaper,
    ContinueObservation,
    PaperAnalyst,
    PaperAnalystValidationError,
    PromotePaperToLive,
)
from fwbg_agents.orchestrator.criteria_paper import CriteriaEvalResult
from fwbg_agents.tools.fwbg_paper_reader import PaperPositions, PaperTradeSummary


def _stub_model(tool_name: str, args: dict) -> FunctionModel:
    """Return a FunctionModel that always emits one final-result tool call."""

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(handler)


def _make_summary(*, slug: str = "demo_v1", days_in_paper: int = 40) -> PaperTradeSummary:
    return PaperTradeSummary(
        strategy_slug=slug,
        sharpe_paper=1.8,
        max_dd_paper=0.12,
        trades_total=120,
        trades_today=2,
        days_in_paper=days_in_paper,
        win_rate=0.58,
        last_trade_at=datetime.now(UTC),
        current_equity=10500.0,
        starting_equity=10000.0,
        equity_curve_sample=[],
    )


def _make_positions(slug: str = "demo_v1") -> PaperPositions:
    return PaperPositions(
        strategy_slug=slug,
        updated_at=datetime.now(UTC),
        positions=[],
    )


def _make_criteria() -> dict:
    return {
        "required_all": [{"sharpe_paper": ">= 1.0"}],
        "hard_blockers": [{"max_dd_paper": "<= 0.3"}],
    }


# ---------------------------------------------------------------------------
# Behaviour 1: Promote passes when paper_criteria_eval.passed is True
# ---------------------------------------------------------------------------


def test_promote_paper_to_live_passes_when_eval_passed():
    model = _stub_model(
        "final_result_PromotePaperToLive",
        {
            "decision": "promote_paper_to_live",
            "rationale": "criteria clear; equity trending up 30+ days",
        },
    )
    analyst = PaperAnalyst(model=model)
    out = analyst.analyze_sync(
        summary=_make_summary(),
        positions=_make_positions(),
        paper_criteria=_make_criteria(),
        paper_phase_target_days=90,
        paper_criteria_eval=CriteriaEvalResult(passed=True, failures=[]),
        strategy_slug="demo_v1",
    )
    assert isinstance(out, PromotePaperToLive)
    assert out.decision == "promote_paper_to_live"


# ---------------------------------------------------------------------------
# Behaviour 2: Promote rejected (validator raises) when eval failed
# ---------------------------------------------------------------------------


def test_promote_paper_to_live_rejected_when_eval_failed():
    model = _stub_model(
        "final_result_PromotePaperToLive",
        {
            "decision": "promote_paper_to_live",
            "rationale": "LLM tried to promote despite failing criteria",
        },
    )
    analyst = PaperAnalyst(model=model)
    with pytest.raises(PaperAnalystValidationError):
        analyst.analyze_sync(
            summary=_make_summary(),
            positions=_make_positions(),
            paper_criteria=_make_criteria(),
            paper_phase_target_days=90,
            paper_criteria_eval=CriteriaEvalResult(
                passed=False, failures=["sharpe_paper: 0.3 fails '>= 1.0'"]
            ),
            strategy_slug="demo_v1",
        )


# ---------------------------------------------------------------------------
# Behaviour 3: Abandon fills default post_mortem_path when LLM omits it
# ---------------------------------------------------------------------------


def test_abandon_paper_fills_default_post_mortem_path(tmp_path):
    model = _stub_model(
        "final_result_AbandonPaper",
        {
            "decision": "abandon_paper",
            "rationale": "persistent loss-bias for 30+ days",
            # post_mortem_path intentionally omitted
        },
    )
    analyst = PaperAnalyst(model=model)
    out = analyst.analyze_sync(
        summary=_make_summary(slug="alpha_v2"),
        positions=_make_positions(slug="alpha_v2"),
        paper_criteria=_make_criteria(),
        paper_phase_target_days=90,
        paper_criteria_eval=CriteriaEvalResult(passed=False, failures=["x"]),
        strategy_slug="alpha_v2",
        data_dir=tmp_path,
    )
    assert isinstance(out, AbandonPaper)
    assert out.post_mortem_path is not None
    assert "alpha_v2" in out.post_mortem_path
    assert out.post_mortem_path.endswith("paper_post_mortem.md")


# ---------------------------------------------------------------------------
# Behaviour 4: ContinueObservation forces stale=True when overdue
# ---------------------------------------------------------------------------


def test_continue_observation_forces_stale_when_overdue():
    model = _stub_model(
        "final_result_ContinueObservation",
        {
            "decision": "continue_observation",
            "rationale": "still borderline",
            "stale": False,  # LLM said False but we're overdue → validator forces True
        },
    )
    analyst = PaperAnalyst(model=model)
    out = analyst.analyze_sync(
        summary=_make_summary(days_in_paper=120),  # > 90 target
        positions=_make_positions(),
        paper_criteria=_make_criteria(),
        paper_phase_target_days=90,
        paper_criteria_eval=CriteriaEvalResult(passed=False, failures=["x"]),
        strategy_slug="demo_v1",
    )
    assert isinstance(out, ContinueObservation)
    assert out.stale is True


# ---------------------------------------------------------------------------
# Behaviour 5: ContinueObservation keeps stale=False when within target
# ---------------------------------------------------------------------------


def test_continue_observation_keeps_stale_false_when_within_target():
    model = _stub_model(
        "final_result_ContinueObservation",
        {
            "decision": "continue_observation",
            "rationale": "still gathering data",
            "stale": False,
        },
    )
    analyst = PaperAnalyst(model=model)
    out = analyst.analyze_sync(
        summary=_make_summary(days_in_paper=40),  # < 90 target
        positions=_make_positions(),
        paper_criteria=_make_criteria(),
        paper_phase_target_days=90,
        paper_criteria_eval=CriteriaEvalResult(passed=False, failures=["x"]),
        strategy_slug="demo_v1",
    )
    assert isinstance(out, ContinueObservation)
    assert out.stale is False
