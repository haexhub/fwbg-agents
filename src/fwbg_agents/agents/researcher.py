"""Researcher agent — LLM-driven strategy-hypothesis generator (M4).

Drives the start of the iteration loop:
- pulls inputs (asset class, family hint, free-text brief)
- calls `lookup_prior_art` (deterministic, anti-redundancy gate)
- optionally calls `search_web` (Tavily, falling back to Brave) for current literature
- emits a typed ResearcherHypothesis

`validate_hypothesis` (orchestrator/hypotheses.py) runs after the LLM
emits its structured output and rejects any hypothesis that conflicts with
prior art without explicitly differentiating itself (design §6.4). The
Researcher cannot bypass this — it is the equivalent of the Analyst's
`validate_and_apply` gate in M3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents import events as event_bus
from fwbg_agents.orchestrator.hypotheses import (
    HypothesisRejectedError,
    ResearcherHypothesis,
    validate_hypothesis,
)
from fwbg_agents.orchestrator.prior_art import PriorArtMatch, lookup_prior_art
from fwbg_agents.persistence.agent_runs import fail_agent_run
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
)
from fwbg_agents.tools.llm import model_for, prompt_path_for
from fwbg_agents.tools.search import SearchProvider, SearchResult, SearchUnavailableError

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "researcher.md"


class ResearcherError(RuntimeError):
    """Raised when the Researcher's hypothesis fails post-LLM validation."""


class ResearcherInput(BaseModel):
    asset_class: str | None = None
    strategy_family_hint: str | None = None
    free_text_brief: str | None = None


def _render_prompt(
    template: str,
    *,
    input: ResearcherInput,
    available_plugins: dict | None = None,
) -> str:
    out = template
    out = out.replace("{{ asset_class }}", input.asset_class or "(asset-agnostic)")
    out = out.replace("{{ strategy_family_hint }}", input.strategy_family_hint or "(none)")
    out = out.replace("{{ free_text_brief }}", input.free_text_brief or "(none)")
    out = out.replace(
        "{{ available_plugins_json }}",
        json.dumps(
            available_plugins
            or {"note": "catalog unavailable — name indicators freely"},
            indent=2,
        ),
    )
    return out


class Researcher:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        search_client: SearchProvider | None = None,
        prompt_path: Path | None = None,
        available_plugins: dict | None = None,
    ):
        self.session = session
        self.model = model if model is not None else model_for("researcher")
        self.search_client = search_client
        self.prompt_path = prompt_path or prompt_path_for("researcher", _PROMPT_PATH)
        # Current fwbg building blocks (fetched by the orchestrator per run) —
        # rendered into the prompt so hypotheses reference real capabilities.
        self.available_plugins = available_plugins

    async def run(self, input: ResearcherInput) -> ResearcherHypothesis:
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="researcher",
            status=AgentRunStatus.RUNNING.value,
            started_at=now,
            created_at=now,
        )
        self.session.add(ar)
        await self.session.commit()
        await self.session.refresh(ar)

        event_bus.emit({
            "type": "agent_run_started",
            "agent_run_id": ar.id,
            "agent_name": "researcher",
        })
        prior_art_seen: list[PriorArtMatch] = []

        try:
            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template, input=input, available_plugins=self.available_plugins
            )

            agent: Agent[None, ResearcherHypothesis] = Agent(
                self.model,
                output_type=ResearcherHypothesis,
                system_prompt=system_prompt,
            )

            session = self.session
            search_client = self.search_client
            agent_run_id = ar.id

            @agent.tool_plain
            async def lookup_prior_art_tool(
                strategy_family: str,
                asset_class: str,
                tags: list[str],
            ) -> list[dict]:
                """Search for prior strategies similar to a proposed one (tag-based,
                anti-redundancy)."""
                matches = await lookup_prior_art(session, strategy_family, asset_class, tags)
                prior_art_seen.extend(matches)
                return [m.model_dump() for m in matches]

            @agent.tool_plain
            async def search_web_tool(query: str) -> list[dict]:
                """Search the web for recent literature on a trading-strategy idea."""
                event_bus.emit({
                    "type": "research_search",
                    "agent_run_id": agent_run_id,
                    "query": query,
                })
                if search_client is None:
                    log.info(
                        "researcher: no search_client configured; skipping search_web('%s')",
                        query,
                    )
                    return []
                try:
                    results: list[SearchResult] = await search_client.search(
                        query, session=session, agent_run_id=agent_run_id
                    )
                except SearchUnavailableError:
                    return []
                except Exception as exc:
                    log.warning("researcher: search_web failed: %s", exc)
                    return []
                serialized = [r.model_dump() for r in results]
                event_bus.emit({
                    "type": "research_results",
                    "agent_run_id": agent_run_id,
                    "query": query,
                    "urls": [{"url": r["url"], "title": r["title"]} for r in serialized],
                })
                return serialized

            t0 = time.monotonic()
            user_msg = "Research and emit your single hypothesis now."
            if input.free_text_brief:
                user_msg = (
                    f"Brief: {input.free_text_brief}\n\n"
                    "Research and emit your single hypothesis now."
                )
            result = await agent.run(user_msg)
            latency_ms = int((time.monotonic() - t0) * 1000)

            usage = result.usage
            self.session.add(
                LlmCall(
                    agent_run_id=ar.id,
                    model=getattr(self.model, "model_name", "unknown"),
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
            )
            await self.session.commit()

            try:
                validate_hypothesis(result.output, prior_art_seen)
            except HypothesisRejectedError as exc:
                raise ResearcherError(str(exc)) from exc

            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            await self.session.commit()
            event_bus.emit({
                "type": "agent_run_done",
                "agent_run_id": ar.id,
                "agent_name": "researcher",
            })

            return result.output
        except asyncio.CancelledError:
            # User cancel: mark the row terminal so it doesn't linger RUNNING
            # (else it looks "stuck" until the janitor sweeps it), then re-raise
            # so the cancellation actually propagates and kills the task.
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = "Cancelled by user"
            with contextlib.suppress(Exception):
                await self.session.commit()
            raise
        except Exception as exc:
            msg = await fail_agent_run(self.session, ar, exc)
            event_bus.emit({
                "type": "agent_run_failed",
                "agent_run_id": ar.id,
                "agent_name": "researcher",
                "error": msg,
            })
            raise
