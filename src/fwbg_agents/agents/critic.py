"""Critic agent — adversarial judge over N Researcher hypothesis candidates (Plan 010 WP3).

Runs only when the research flow collects more than one candidate hypothesis
(``settings.researcher_candidates_n > 1``). The Critic never proposes ideas —
it scores the given candidates and picks one winner, or rejects the whole
batch. It cannot force a run to succeed: if every candidate is rejected, the
caller (``orchestrator/research_flow.py``) raises
``ResearcherFanoutExhaustedError`` exactly as it would with zero valid
hypotheses today.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.instrumented import run_instrumented
from fwbg_agents.orchestrator.hypotheses import ResearcherHypothesis
from fwbg_agents.orchestrator.lessons import lessons_digest
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.models import AgentRunStatus, LlmCall
from fwbg_agents.tools.llm import model_for, prompt_path_for
from fwbg_agents.tools.llm_pricing import estimate_cost_usd

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "critic.md"


class CriticCandidate(BaseModel):
    """The Critic's verdict on one candidate hypothesis."""

    score: float = Field(ge=0.0, le=1.0)
    kill_risks: list[str]
    verdict: Literal["pass", "reject"]


class CriticReport(BaseModel):
    """The Critic's full judgement over all candidates, in input order.

    ``winner_index`` is null when every candidate is rejected; when set it
    should point at a ``pass`` candidate, but the caller re-derives a winner
    from the highest-scoring ``pass`` candidate if it doesn't (a single
    inconsistent auxiliary field shouldn't fail an otherwise-usable batch).
    """

    candidates: list[CriticCandidate]
    winner_index: int | None = None


def _render_prompt(template: str, *, hypotheses: list[ResearcherHypothesis]) -> str:
    out = template
    out = out.replace("{{ n_candidates }}", str(len(hypotheses)))
    out = out.replace(
        "{{ candidates_json }}",
        json.dumps([h.model_dump(mode="json") for h in hypotheses], indent=2),
    )
    out = out.replace("{{ lessons_digest }}", lessons_digest())
    return out


class Critic:
    """LLM-driven adversarial judge that scores candidate hypotheses and
    picks a winner (or rejects the whole batch)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
    ):
        """Initialize."""
        self.session = session
        self.model = model if model is not None else model_for("critic")
        self.prompt_path = prompt_path or prompt_path_for("critic", _PROMPT_PATH)

    async def judge(self, hypotheses: list[ResearcherHypothesis]) -> tuple[CriticReport, int]:
        """Score every candidate hypothesis. Returns (report, agent_run_id).

        The agent_run_id is returned (not embedded in CriticReport) so the
        LLM's output schema stays free of fields it has no business emitting.
        """
        if not hypotheses:
            raise ValueError("Critic.judge requires at least one candidate hypothesis")

        ar = await start_agent_run(self.session, agent_name="critic")
        try:
            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(template, hypotheses=hypotheses)

            agent: Agent[None, CriticReport] = Agent(
                self.model,
                output_type=CriticReport,
                system_prompt=system_prompt,
                retries={"output": 3},
            )
            t0 = time.monotonic()
            result = await run_instrumented(agent, "Emit your critique now.", agent_run_id=ar.id)
            latency_ms = int((time.monotonic() - t0) * 1000)

            usage = result.usage
            model_name = getattr(self.model, "model_name", "unknown")
            in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            self.session.add(
                LlmCall(
                    agent_run_id=ar.id,
                    model=model_name,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cost_usd=estimate_cost_usd(model_name, in_tokens, out_tokens),
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
            )

            report = result.output
            if len(report.candidates) != len(hypotheses):
                raise ValueError(
                    f"critic returned {len(report.candidates)} verdicts for "
                    f"{len(hypotheses)} candidates"
                )

            await finish_agent_run(self.session, ar, status=AgentRunStatus.DONE)
            return report, ar.id
        except Exception as exc:
            await fail_agent_run(self.session, ar, exc)
            raise
