"""Translator agent — turns ResearcherHypothesis into a runnable fwbg strategy.json (M4).

Two modes:
- `run_fresh(strategy)` — LLM-driven. Reads iteration_001/hypothesis.json,
  emits strategy.json + spec.md. Strategy stays PROPOSED — the Runner picks
  it up next.
- `run_reiterate(parent)` — deterministic (no LLM). Reads the parent's
  analyst_recommendation.json sidecar and produces a child Strategy with
  parent_strategy_id set, iteration_count=1, and the recommendation
  applied. Stays PROPOSED.

Both paths run `validate_strategy_json` on the result and refuse to commit
if the structural check fails — better to mark the AgentRun failed than to
write a broken file that the Runner would only catch later.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.hypotheses import generate_slug
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.plugin_catalog import load_catalog
from fwbg_agents.orchestrator.strategy_validator import (
    KNOWN_DATASOURCES,
    KNOWN_FILTERS,
    KNOWN_MODELS,
    KNOWN_PIPELINES,
    KNOWN_RESOURCES,
    KNOWN_TIMEFRAMES,
    KNOWN_VALIDATIONS,
    StrategyValidationError,
    validate_strategy_json,
)
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)
from fwbg_agents.tools.llm import default_model

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "translator.md"

# Maps Analyst AddIndicator.phase strings → strategy.json list-field names.
# Used by `run_reiterate_with_plugin` when splicing a VERIFIED plugin slug
# into a child Strategy. Module-scope so tests/callers can reference it.
_PHASE_TO_FIELD: dict[str, str] = {
    "indicator": "indicators",
    "feature_selection": "feature_selection",
    "preprocessing": "preprocessing",
    "filter": "extra_filters",
}


class TranslatorError(RuntimeError):
    """Raised when the Translator output fails structural validation."""


class _TranslatorOutput(BaseModel):
    """Loose envelope for the LLM's strategy.json so pydantic-ai can hand us
    structured output. The downstream validator does the strict check."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    hypothesis: str = ""
    expected_outcome: str = ""
    datasource: str
    pipeline: str
    model: str
    filters: str
    validation: str
    resources: str
    timeframe: str
    exit_strategies: list[dict] = []
    tags: list[str] = []
    optimization: dict = {}


def _render_prompt(template: str, *, hypothesis_json: str, known_plugins_json: str) -> str:
    return template.replace("{{ hypothesis_json }}", hypothesis_json).replace(
        "{{ known_plugins_json }}", known_plugins_json
    )


def _known_plugins_dict() -> dict[str, list[str]]:
    return {
        "datasource": sorted(KNOWN_DATASOURCES),
        "pipeline": sorted(KNOWN_PIPELINES),
        "model": sorted(KNOWN_MODELS),
        "filters": sorted(KNOWN_FILTERS),
        "validation": sorted(KNOWN_VALIDATIONS),
        "resources": sorted(KNOWN_RESOURCES),
        "timeframe": sorted(KNOWN_TIMEFRAMES),
    }


def _write_spec_md(
    path: Path, *, strategy_slug: str, hypothesis: dict, strategy_json: dict
) -> None:
    plugins = ", ".join(
        f"{k}={strategy_json.get(k)}"
        for k in ("pipeline", "model", "filters", "validation", "resources")
    )
    text = (
        f"# Spec — {strategy_slug}\n\n"
        "## Goal\n\n"
        f"{hypothesis.get('hypothesis', '').strip()}\n\n"
        "## Inputs\n\n"
        f"- asset_class: `{hypothesis.get('asset_class')}`\n"
        f"- strategy_family: `{hypothesis.get('strategy_family')}`\n"
        f"- timeframe: `{strategy_json.get('timeframe')}`\n"
        f"- key_indicators: {hypothesis.get('key_indicators', [])}\n\n"
        "## Outputs\n\n"
        f"- fwbg `strategy.json` with {plugins}\n"
        f"- Backtest run (via Runner) producing `fwbg_results.json`\n"
        f"- Analyst report and recommendation\n\n"
        "## Acceptance Criteria\n\n"
        f"- {hypothesis.get('expected_edge_explanation', '').strip()}\n"
        f"- Promotion gates per `criteria/{hypothesis.get('asset_class')}.yaml`\n\n"
        "## Implementation Notes\n\n"
        f"- Tags: {strategy_json.get('tags', [])}\n"
        f"- Exit strategies: {len(strategy_json.get('exit_strategies', []))} configured\n"
    )
    path.write_text(text)


class Translator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
    ):
        self.session = session
        self.model = model if model is not None else default_model()
        self.prompt_path = prompt_path or _PROMPT_PATH

    async def run_fresh(self, strategy: Strategy) -> Path:
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="translator",
            status=AgentRunStatus.RUNNING.value,
            strategy_id=strategy.id,
            started_at=now,
            created_at=now,
        )
        self.session.add(ar)
        await self.session.commit()
        await self.session.refresh(ar)

        try:
            iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            hypothesis_path = iteration_dir / "hypothesis.json"
            if not hypothesis_path.is_file():
                raise FileNotFoundError(f"missing hypothesis.json at {hypothesis_path}")

            ar.input_artifact_path = str(hypothesis_path)
            hypothesis_data = json.loads(hypothesis_path.read_text())

            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template,
                hypothesis_json=json.dumps(hypothesis_data, indent=2),
                known_plugins_json=json.dumps(_known_plugins_dict(), indent=2),
            )

            agent: Agent[None, _TranslatorOutput] = Agent(
                self.model, output_type=_TranslatorOutput, system_prompt=system_prompt
            )

            @agent.tool_plain
            def get_known_plugins() -> dict[str, list[str]]:
                """Return the catalog of plugin slugs the Translator may pick from."""
                return _known_plugins_dict()

            t0 = time.monotonic()
            result = await agent.run("Emit the strategy.json now.")
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

            payload = result.output.model_dump()
            payload["name"] = strategy.slug  # canonical slug wins

            try:
                validate_strategy_json(payload)
            except StrategyValidationError as exc:
                raise TranslatorError(str(exc)) from exc

            strategy_path = iteration_dir / "strategy.json"
            strategy_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

            spec_path = iteration_dir / "spec.md"
            _write_spec_md(
                spec_path,
                strategy_slug=strategy.slug,
                hypothesis=hypothesis_data,
                strategy_json=payload,
            )
            strategy.spec_path = str(spec_path)
            strategy.updated_at = datetime.now(UTC)

            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(strategy_path)
            await self.session.commit()

            return strategy_path
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise

    async def run_reiterate(self, parent: Strategy) -> Strategy:
        """Apply an Analyst recommendation sidecar deterministically and create a child Strategy.

        Deterministic by design — no LLM. TuneParams replaces an entry in
        `optimization.grid_params`; ChangeExit swaps `exit_strategies` with the
        sidecar's `new_exit_strategy`. Parent stays in its current state; child
        is a fresh PROPOSED row with `parent_strategy_id=parent.id`.

        Sidecar JSON shape (mirroring `recommendations._rec_to_dict`):
        - tune_params: {kind, confidence, reasoning, param, new_range}
        - change_exit: {kind, confidence, reasoning, from_exit, to_exit, new_exit_strategy}
        """
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="translator",
            status=AgentRunStatus.RUNNING.value,
            strategy_id=parent.id,
            started_at=now,
            created_at=now,
        )
        self.session.add(ar)
        await self.session.commit()
        await self.session.refresh(ar)

        try:
            parent_dir = strategy_dir(parent.slug) / "iteration_001"
            sidecar_path = parent_dir / "analyst_recommendation.json"
            parent_strategy_path = parent_dir / "strategy.json"
            parent_hypothesis_path = parent_dir / "hypothesis.json"

            if not sidecar_path.is_file():
                raise TranslatorError(
                    f"missing analyst_recommendation.json at {sidecar_path}"
                )
            if not parent_strategy_path.is_file():
                raise TranslatorError(
                    f"parent missing strategy.json at {parent_strategy_path}"
                )
            ar.input_artifact_path = str(sidecar_path)

            rec = json.loads(sidecar_path.read_text())
            parent_payload = json.loads(parent_strategy_path.read_text())

            child_payload = json.loads(json.dumps(parent_payload))  # deep copy
            kind = rec.get("kind")
            if kind == "tune_params":
                param = rec.get("param")
                new_range = rec.get("new_range")
                if not param or not isinstance(new_range, list):
                    raise TranslatorError(
                        f"tune_params sidecar missing param/new_range: {rec}"
                    )
                grid = child_payload.setdefault("optimization", {}).setdefault(
                    "grid_params", {}
                )
                grid[param] = new_range
            elif kind == "change_exit":
                new_exit = rec.get("new_exit_strategy")
                if not isinstance(new_exit, dict):
                    raise TranslatorError(
                        "change_exit sidecar missing new_exit_strategy (the Analyst must "
                        "populate it for M4 reiterate; see ChangeExit.new_exit_strategy)"
                    )
                child_payload["exit_strategies"] = [new_exit]
            else:
                raise TranslatorError(
                    f"reiterate only handles tune_params/change_exit, got kind={kind!r}"
                )

            child_slug = await generate_slug(
                self.session, parent.strategy_family, parent.asset_class
            )
            child_payload["name"] = child_slug

            try:
                validate_strategy_json(child_payload)
            except StrategyValidationError as exc:
                raise TranslatorError(str(exc)) from exc

            now2 = datetime.now(UTC)
            child = Strategy(
                slug=child_slug,
                current_state=StrategyState.PROPOSED.value,
                iteration_count=1,
                parent_strategy_id=parent.id,
                asset_class=parent.asset_class,
                strategy_family=parent.strategy_family,
                created_at=now2,
                updated_at=now2,
            )
            self.session.add(child)
            await self.session.flush()

            # Copy parent tags onto child for lineage / prior-art discoverability.
            parent_tags = parent_payload.get("tags") or []
            for tag in parent_tags:
                self.session.add(StrategyTag(strategy_id=child.id, tag=tag))

            child_dir = strategy_dir(child.slug) / "iteration_001"
            child_dir.mkdir(parents=True, exist_ok=True)
            (child_dir / "strategy.json").write_text(
                json.dumps(child_payload, indent=2, sort_keys=True)
            )

            if parent_hypothesis_path.is_file():
                hypothesis_data = json.loads(parent_hypothesis_path.read_text())
                (child_dir / "hypothesis.json").write_text(
                    json.dumps(hypothesis_data, indent=2)
                )
                child.hypothesis_path = str(child_dir / "hypothesis.json")
            else:
                hypothesis_data = {
                    "title": f"Re-iteration of {parent.slug}",
                    "asset_class": parent.asset_class,
                    "strategy_family": parent.strategy_family,
                    "hypothesis": "(inherited)",
                }

            spec_path = child_dir / "spec.md"
            _write_spec_md(
                spec_path,
                strategy_slug=child.slug,
                hypothesis=hypothesis_data,
                strategy_json=child_payload,
            )
            child.spec_path = str(spec_path)

            self.session.add(
                Transition(
                    entity_type="strategy",
                    entity_id=child.id,
                    from_state=None,
                    to_state=StrategyState.PROPOSED.value,
                    reason=f"translator: re-iterate from {parent.slug} ({kind})",
                    payload={
                        "parent_strategy_id": parent.id,
                        "recommendation_kind": kind,
                        "recommendation": rec,
                    },
                    created_by="translator",
                    created_at=now2,
                )
            )

            ar.strategy_id = child.id
            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(child_dir / "strategy.json")
            await self.session.commit()
            await self.session.refresh(child)
            return child
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise

    async def run_reiterate_with_plugin(
        self,
        parent: Strategy,
        plugin_slug: str,
        sidecar: dict,
    ) -> Strategy:
        """Splice a VERIFIED plugin slug into a child Strategy (deterministic).

        Mirrors `run_reiterate` for `add_indicator` recommendations: maps the
        sidecar `phase` to one of the four M5c plugin-slot list-fields and
        appends the slug. Plugin VERIFIED check is the caller's job — we
        validate via `validate_strategy_json(..., catalog=load_catalog())`,
        which rejects any slug not visible in the catalog.

        Parent stays in BACKTESTED; child is a fresh PROPOSED row with
        `parent_strategy_id=parent.id`. Phase-to-field mapping:
            "indicator"          -> indicators
            "feature_selection"  -> feature_selection
            "preprocessing"      -> preprocessing
            "filter"             -> extra_filters
        """
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="translator",
            status=AgentRunStatus.RUNNING.value,
            strategy_id=parent.id,
            started_at=now,
            created_at=now,
        )
        self.session.add(ar)
        await self.session.commit()
        await self.session.refresh(ar)

        try:
            if parent.current_state != StrategyState.BACKTESTED.value:
                raise TranslatorError(
                    f"reiterate_with_plugin requires parent in BACKTESTED, "
                    f"got {parent.current_state}"
                )

            phase = sidecar.get("phase")
            if phase not in _PHASE_TO_FIELD:
                raise TranslatorError(
                    f"unknown phase: {phase!r} (must be one of: "
                    "indicator, feature_selection, preprocessing, filter)"
                )
            list_field = _PHASE_TO_FIELD[phase]

            parent_dir = strategy_dir(parent.slug) / "iteration_001"
            parent_strategy_path = parent_dir / "strategy.json"
            parent_hypothesis_path = parent_dir / "hypothesis.json"
            sidecar_input_path = parent_dir / "add_indicator_request.json"

            if not parent_strategy_path.is_file():
                raise TranslatorError(
                    f"parent missing strategy.json at {parent_strategy_path}"
                )
            ar.input_artifact_path = str(sidecar_input_path)

            parent_payload = json.loads(parent_strategy_path.read_text())
            child_payload = json.loads(json.dumps(parent_payload))  # deep copy
            child_payload.setdefault(list_field, []).append(plugin_slug)

            child_slug = await generate_slug(
                self.session, parent.strategy_family, parent.asset_class
            )
            child_payload["name"] = child_slug

            catalog = await load_catalog(self.session)
            try:
                validate_strategy_json(child_payload, catalog=catalog)
            except StrategyValidationError as exc:
                raise TranslatorError(str(exc)) from exc

            now2 = datetime.now(UTC)
            child = Strategy(
                slug=child_slug,
                current_state=StrategyState.PROPOSED.value,
                iteration_count=1,
                parent_strategy_id=parent.id,
                asset_class=parent.asset_class,
                strategy_family=parent.strategy_family,
                created_at=now2,
                updated_at=now2,
            )
            self.session.add(child)
            await self.session.flush()

            parent_tags = parent_payload.get("tags") or []
            for tag in parent_tags:
                self.session.add(StrategyTag(strategy_id=child.id, tag=tag))

            child_dir = strategy_dir(child.slug) / "iteration_001"
            child_dir.mkdir(parents=True, exist_ok=True)
            strategy_path = child_dir / "strategy.json"
            strategy_path.write_text(json.dumps(child_payload, indent=2, sort_keys=True))

            # Hypothesis inheritance + iteration annotation (M5c Decision C1).
            capability = sidecar.get("capability", "")
            child_hypothesis: dict
            if parent_hypothesis_path.is_file():
                parent_hypothesis_raw = json.loads(parent_hypothesis_path.read_text())
                if isinstance(parent_hypothesis_raw, dict):
                    child_hypothesis = json.loads(json.dumps(parent_hypothesis_raw))
                    existing = child_hypothesis.get("iterations")
                    if isinstance(existing, list):
                        iter_num = len(existing) + 1
                        existing.append(
                            {
                                "iteration": iter_num,
                                "action": "add_indicator",
                                "plugin_slug": plugin_slug,
                                "phase": phase,
                                "capability": capability,
                                "rationale": (
                                    f"Iteration {iter_num}: added {plugin_slug} at "
                                    f"{phase} per analyst recommendation: {capability}"
                                ),
                            }
                        )
                    else:
                        child_hypothesis["iterations"] = [
                            {
                                "iteration": 1,
                                "action": "add_indicator",
                                "plugin_slug": plugin_slug,
                                "phase": phase,
                                "capability": capability,
                                "rationale": (
                                    f"Iteration 1: added {plugin_slug} at {phase} "
                                    f"per analyst recommendation: {capability}"
                                ),
                            }
                        ]
                else:
                    # Legacy: parent hypothesis stored as a bare string.
                    child_hypothesis = {
                        "inherited_text": str(parent_hypothesis_raw),
                        "iterations": [
                            {
                                "iteration": 1,
                                "action": "add_indicator",
                                "plugin_slug": plugin_slug,
                                "phase": phase,
                                "capability": capability,
                                "rationale": (
                                    f"Iteration 1: added {plugin_slug} at {phase} "
                                    f"per analyst recommendation: {capability}"
                                ),
                            }
                        ],
                    }
            else:
                child_hypothesis = {
                    "inherited_from": parent.slug,
                    "iterations": [
                        {
                            "iteration": 1,
                            "action": "add_indicator",
                            "plugin_slug": plugin_slug,
                            "phase": phase,
                            "capability": capability,
                            "rationale": (
                                f"Iteration 1: added {plugin_slug} at {phase} "
                                f"per analyst recommendation: {capability}"
                            ),
                        }
                    ],
                }

            hypothesis_path = child_dir / "hypothesis.json"
            hypothesis_path.write_text(
                json.dumps(child_hypothesis, indent=2, sort_keys=False)
            )
            child.hypothesis_path = str(hypothesis_path)

            spec_path = child_dir / "spec.md"
            _write_spec_md(
                spec_path,
                strategy_slug=child.slug,
                hypothesis=child_hypothesis,
                strategy_json=child_payload,
            )
            child.spec_path = str(spec_path)
            child.updated_at = datetime.now(UTC)

            self.session.add(
                Transition(
                    entity_type="strategy",
                    entity_id=child.id,
                    from_state=None,
                    to_state=StrategyState.PROPOSED.value,
                    reason="translator: reiterate_with_plugin",
                    payload={
                        "parent_strategy_id": parent.id,
                        "plugin_slug": plugin_slug,
                        "sidecar": sidecar,
                    },
                    created_by="translator",
                    created_at=now2,
                )
            )

            ar.strategy_id = child.id
            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(strategy_path)
            await self.session.commit()
            await self.session.refresh(child)
            return child
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise
