"""PaperAnalyst agent — paper-trading decision engine (M6b Task 4).

Reads paper-trading telemetry (PaperTradeSummary + PaperPositions) plus a
pre-computed CriteriaEvalResult and emits one of three structured decisions:

  PromotePaperToLive | AbandonPaper | ContinueObservation

Structured output is enforced by pydantic-ai. A deterministic post-LLM
validator then runs hard rules:

  - PromotePaperToLive with `paper_criteria_eval.passed is False` → raise
    PaperAnalystValidationError (the LLM cannot bypass safety gates).
  - AbandonPaper without `post_mortem_path` → fill the default path
    `<data_dir>/strategies/<slug>/paper_post_mortem.md`.
  - ContinueObservation when `summary.days_in_paper > paper_phase_target_days`
    → force `stale=True`.

The PaperAnalyst NEVER transitions state — it only emits a typed
recommendation. The orchestrator (M6b Task 5) handles persistence.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator
from pydantic_ai import Agent
from pydantic_ai.models import Model

from fwbg_agents.orchestrator.criteria_paper import CriteriaEvalResult
from fwbg_agents.tools.fwbg_paper_reader import PaperPositions, PaperTradeSummary
from fwbg_agents.tools.llm import default_model


# ---------------------------------------------------------------------------
# Discriminated-union output schema
# ---------------------------------------------------------------------------


class PromotePaperToLive(BaseModel):
    decision: Literal["promote_paper_to_live"] = "promote_paper_to_live"
    rationale: str


class AbandonPaper(BaseModel):
    decision: Literal["abandon_paper"] = "abandon_paper"
    rationale: str
    post_mortem_path: str | None = None


class ContinueObservation(BaseModel):
    decision: Literal["continue_observation"] = "continue_observation"
    rationale: str
    stale: bool = False


PaperAnalystOutput = Annotated[
    PromotePaperToLive | AbandonPaper | ContinueObservation,
    Discriminator("decision"),
]


class PaperAnalystValidationError(Exception):
    """Raised when the LLM emits a decision that violates a hard rule."""


# ---------------------------------------------------------------------------


_PROMPT_PATH = Path(__file__).parent / "prompts" / "paper_analyst.md"


class PaperAnalyst:
    def __init__(
        self,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
    ):
        self.model = model if model is not None else default_model()
        self.prompt_path = prompt_path if prompt_path is not None else _PROMPT_PATH

    def analyze_sync(
        self,
        *,
        summary: PaperTradeSummary,
        positions: PaperPositions,
        paper_criteria: dict,
        paper_phase_target_days: int,
        paper_criteria_eval: CriteriaEvalResult,
        strategy_slug: str,
        data_dir: Path | None = None,
    ) -> PromotePaperToLive | AbandonPaper | ContinueObservation:
        system_prompt = self.prompt_path.read_text()
        agent = Agent(
            self.model,
            output_type=PaperAnalystOutput,
            system_prompt=system_prompt,
        )
        user_payload = {
            "summary": summary.model_dump(mode="json"),
            "positions": positions.model_dump(mode="json"),
            "paper_criteria": paper_criteria,
            "paper_phase_target_days": paper_phase_target_days,
            "paper_criteria_eval": asdict(paper_criteria_eval),
        }
        result = agent.run_sync(json.dumps(user_payload, indent=2, default=str))
        return self._validate(
            result.output,
            summary=summary,
            paper_phase_target_days=paper_phase_target_days,
            paper_criteria_eval=paper_criteria_eval,
            strategy_slug=strategy_slug,
            data_dir=data_dir,
        )

    def _validate(
        self,
        out: PromotePaperToLive | AbandonPaper | ContinueObservation,
        *,
        summary: PaperTradeSummary,
        paper_phase_target_days: int,
        paper_criteria_eval: CriteriaEvalResult,
        strategy_slug: str,
        data_dir: Path | None,
    ) -> PromotePaperToLive | AbandonPaper | ContinueObservation:
        if isinstance(out, PromotePaperToLive):
            if not paper_criteria_eval.passed:
                raise PaperAnalystValidationError(
                    f"PaperAnalyst tried to promote {strategy_slug} but "
                    f"paper_criteria_eval.passed is False "
                    f"(failures={paper_criteria_eval.failures})"
                )
            return out

        if isinstance(out, AbandonPaper):
            if out.post_mortem_path is None:
                base = data_dir if data_dir is not None else Path("data")
                default_path = (
                    base / "strategies" / strategy_slug / "paper_post_mortem.md"
                )
                return out.model_copy(update={"post_mortem_path": str(default_path)})
            return out

        # ContinueObservation
        if summary.days_in_paper > paper_phase_target_days and not out.stale:
            return out.model_copy(update={"stale": True})
        return out
