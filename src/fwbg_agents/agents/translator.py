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

from fwbg_agents.agents.instrumented import run_instrumented
from fwbg_agents.orchestrator.hypotheses import generate_child_slug
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.live_catalog import LiveCatalog, fetch_live_catalog
from fwbg_agents.orchestrator.strategy_validator import (
    KNOWN_RESOURCES,
    KNOWN_VALIDATIONS,
    StrategyValidationError,
    validate_strategy_json,
)
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.models import (
    AgentRunStatus,
    LlmCall,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)
from fwbg_agents.tools.fwbg_client import FwbgClient
from fwbg_agents.tools.llm import model_for, prompt_path_for

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


_INLINE_PIPELINE_PHASES = ("indicators", "preprocessing", "feature_selection", "data_loading")

# Sections a ModifyPlugins op may touch. The first three live inside an inline
# `pipeline` dict as {name, params} entries; `extra_filters` (and any legacy
# preset-string pipeline) is a top-level list of plain slugs.
_MODIFY_INLINE_SECTIONS = ("indicators", "preprocessing", "feature_selection")
_MODIFY_SECTIONS = (*_MODIFY_INLINE_SECTIONS, "extra_filters")


def _apply_plugin_op(payload: dict, op: dict) -> None:
    """Apply one ModifyPlugins op to a strategy payload in place.

    Deterministic — no LLM. Raises TranslatorError on any inconsistency
    (unknown section, slug not present for remove/replace, duplicate add);
    catalog membership of the new slug is checked later by
    `validate_strategy_json` against the live catalog.
    """
    action = op.get("action")
    section = op.get("section")
    slug = op.get("slug")
    params = op.get("params") or {}
    replaces = op.get("replaces")

    if action not in ("add", "remove", "replace") or not isinstance(slug, str) or not slug:
        raise TranslatorError(f"invalid modify_plugins op: {op}")
    if section not in _MODIFY_SECTIONS:
        raise TranslatorError(
            f"modify_plugins: unknown section {section!r} (must be one of {list(_MODIFY_SECTIONS)})"
        )
    if action == "replace" and (not isinstance(replaces, str) or not replaces):
        raise TranslatorError(f"modify_plugins: replace op needs 'replaces': {op}")

    pipeline = payload.get("pipeline")
    if section in _MODIFY_INLINE_SECTIONS and isinstance(pipeline, dict):
        entries = pipeline.setdefault(section, [])

        def _idx(name: str) -> int | None:
            """Return the index of the entry with matching name, or None."""
            for i, e in enumerate(entries):
                if isinstance(e, dict) and e.get("name") == name:
                    return i
            return None

        if action == "add":
            if _idx(slug) is not None:
                raise TranslatorError(
                    f"modify_plugins: {slug!r} already present in pipeline.{section}"
                )
            entries.append({"name": slug, "params": params})
        elif action == "remove":
            i = _idx(slug)
            if i is None:
                raise TranslatorError(f"modify_plugins: {slug!r} not found in pipeline.{section}")
            entries.pop(i)
        else:  # replace
            i = _idx(replaces)  # type: ignore[arg-type]  # validated as non-empty str above
            if i is None:
                raise TranslatorError(
                    f"modify_plugins: {replaces!r} not found in pipeline.{section}"
                )
            entries[i] = {"name": slug, "params": params}
        return

    # Top-level slug-list fields: extra_filters always, and the pipeline
    # sections when the pipeline is a legacy preset string.
    entries = payload.setdefault(section, [])
    if not isinstance(entries, list):
        raise TranslatorError(f"modify_plugins: {section} is not a list")
    if params:
        log.warning(
            "modify_plugins: params for %r dropped — %s is a plain slug list",
            slug,
            section,
        )
    if action == "add":
        if slug in entries:
            raise TranslatorError(f"modify_plugins: {slug!r} already present in {section}")
        entries.append(slug)
    elif action == "remove":
        if slug not in entries:
            raise TranslatorError(f"modify_plugins: {slug!r} not found in {section}")
        entries.remove(slug)
    else:  # replace
        if replaces not in entries:
            raise TranslatorError(f"modify_plugins: {replaces!r} not found in {section}")
        entries[entries.index(replaces)] = slug


def _child_universe(parent: Strategy, target_assets: list[str]) -> list | None:
    """suggested_universe for a re-iteration child.

    With `target_assets` the child's universe narrows to exactly those
    symbols (keeping the parent's per-symbol timeframe hints where present);
    otherwise the parent's universe is inherited unchanged.
    """
    if not target_assets:
        return parent.suggested_universe
    tf_by_symbol: dict[str, str] = {}
    for e in parent.suggested_universe or []:
        if isinstance(e, dict) and e.get("scope") == "symbol":
            value, tf = e.get("value"), e.get("timeframe")
            if isinstance(value, str) and isinstance(tf, str) and tf:
                tf_by_symbol.setdefault(value, tf)
    universe: list[dict] = []
    for sym in target_assets:
        entry: dict = {"scope": "symbol", "value": sym}
        if tf_by_symbol.get(sym):
            entry["timeframe"] = tf_by_symbol[sym]
        universe.append(entry)
    return universe


def _validate_inline_params(
    strategy: dict,
    plugin_schemas: list[dict],
) -> None:
    """Validate inline pipeline param values against each plugin's schema options.

    Raises StrategyValidationError when a param value is not in the schema's
    `options` list. Silent when schemas are unavailable (graceful degradation).
    """
    schema_by_name: dict[str, dict] = {
        p["name"]: p.get("param_schema") or {}
        for p in plugin_schemas
        if isinstance(p.get("name"), str)
    }
    pipeline = strategy.get("pipeline") or {}
    if not isinstance(pipeline, dict):
        return
    for phase in _INLINE_PIPELINE_PHASES:
        entries = pipeline.get(phase)
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            param_schema = schema_by_name.get(name)
            if not param_schema:
                continue
            params = entry.get("params") or {}
            for param_name, value in params.items():
                field_schema = param_schema.get(param_name)
                if not isinstance(field_schema, dict):
                    continue
                options = field_schema.get("options") or field_schema.get("choices")
                if not options:
                    continue
                values = value if isinstance(value, list) else [value]
                # Options come from the plugin schema as strings (e.g. session
                # ids "0".."15"); the model often emits them as ints. Compare
                # by string form so a valid `sessions: [0]` isn't rejected.
                allowed = {str(o) for o in options}
                bad = [v for v in values if str(v) not in allowed]
                if bad:
                    raise StrategyValidationError(
                        f"pipeline.{phase}[{i}] ({name!r}): param {param_name!r} "
                        f"contains invalid value(s) {bad}. "
                        f"Valid options: {options}"
                    )


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
    # Inline composition (dict) is the M7 default; string preset refs stay
    # accepted for backwards compatibility. The validator does the strict check.
    pipeline: dict | str
    model: dict | str
    filters: dict | str
    validation: str
    resources: str
    timeframe: str
    exit_strategies: list[dict] = []
    tags: list[str] = []
    optimization: dict = {}


def _render_prompt(template: str, *, hypothesis_json: str, known_plugins_json: str) -> str:
    """Render the translator prompt template with hypothesis and plugin catalog JSON."""
    return template.replace("{{ hypothesis_json }}", hypothesis_json).replace(
        "{{ known_plugins_json }}", known_plugins_json
    )


def _catalog_prompt_dict(live: LiveCatalog) -> dict:
    """Render the live catalog into the prompt's `known_plugins_json` blob.

    Per-plugin default_params double as parameter documentation — the LLM
    composes real params from them instead of guessing names.
    """
    composable = {
        category: live.plugin_details.get(category, [])
        for category in (
            "indicators",
            "preprocessing",
            "feature_selection",
            "data_loading",
            "models",
            "exit_strategies",
        )
    }
    return {
        **composable,
        "exit_modifiers": live.exit_modifiers,
        "entry_modifiers": live.entry_modifiers,
        "validation_presets": live.presets.get("validations") or sorted(KNOWN_VALIDATIONS),
        "resources_presets": live.presets.get("resources") or sorted(KNOWN_RESOURCES),
        # Configured datasources (their asset lists = CURRENT downloads; more
        # is fetched on demand) plus the full downloadable asset registry.
        "datasources": live.datasources,
        "asset_registry": live.asset_registry,
        "timeframes": live.timeframes,
    }


def _write_spec_md(
    path: Path, *, strategy_slug: str, hypothesis: dict, strategy_json: dict
) -> None:
    """Write a human-readable spec.md summarising the strategy and its hypothesis."""
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
    """LLM-driven agent that turns a ResearcherHypothesis into a runnable fwbg strategy.json."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
        fwbg_client: FwbgClient | None = None,
    ):
        """Initialize."""
        self.session = session
        self.model = model if model is not None else model_for("translator")
        self.prompt_path = prompt_path or prompt_path_for("translator", _PROMPT_PATH)
        # Used to fetch the CURRENT plugin/preset catalog at run time; without
        # it fetch_live_catalog degrades to the local filesystem scan.
        self.fwbg_client = fwbg_client

    async def run_fresh(self, strategy: Strategy) -> Path:
        """Translate a hypothesis into strategy.json via LLM and validate the result."""
        ar = await start_agent_run(self.session, agent_name="translator", strategy_id=strategy.id)

        try:
            iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            hypothesis_path = iteration_dir / "hypothesis.json"
            if not hypothesis_path.is_file():
                raise FileNotFoundError(f"missing hypothesis.json at {hypothesis_path}")

            ar.input_artifact_path = str(hypothesis_path)
            hypothesis_data = json.loads(hypothesis_path.read_text())

            # Fetched fresh per run — new plugins/presets must be visible
            # immediately, not at the next deploy.
            live = await fetch_live_catalog(self.session, self.fwbg_client)
            catalog_prompt = _catalog_prompt_dict(live)

            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template,
                hypothesis_json=json.dumps(hypothesis_data, indent=2),
                known_plugins_json=json.dumps(catalog_prompt, indent=2),
            )

            agent: Agent[None, _TranslatorOutput] = Agent(
                self.model, output_type=_TranslatorOutput, system_prompt=system_prompt
            )

            @agent.tool_plain
            def get_known_plugins() -> dict:
                """Return the catalog of plugins the Translator may compose from."""
                return catalog_prompt

            t0 = time.monotonic()
            result = await run_instrumented(
                agent, "Emit the strategy.json now.", agent_run_id=ar.id
            )
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
                validate_strategy_json(
                    payload,
                    catalog=live.catalog,
                    presets=live.presets,
                    datasources=live.datasource_names() or None,
                    timeframes=live.timeframes or None,
                )
                if self.fwbg_client is not None:
                    try:
                        plugin_schemas = await self.fwbg_client.get_plugins()
                        _validate_inline_params(payload, plugin_schemas)
                    except StrategyValidationError:
                        raise
                    except Exception as exc:
                        log.warning("could not validate inline params (non-fatal): %s", exc)
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

            await finish_agent_run(
                self.session,
                ar,
                status=AgentRunStatus.DONE,
                output_artifact_path=str(strategy_path),
            )

            return strategy_path
        except Exception as exc:
            await fail_agent_run(self.session, ar, exc)
            raise

    async def run_reiterate(self, parent: Strategy) -> Strategy:
        """Apply an Analyst recommendation sidecar deterministically and create a child Strategy.

        Deterministic by design — no LLM. TuneParams replaces entries in
        `optimization.grid_params`; ChangeExit swaps `exit_strategies` with the
        sidecar's `new_exit_strategy`; ModifyPlugins applies its ops to the
        plugin composition. Parent stays in its current state; child is a
        fresh PROPOSED row with `parent_strategy_id=parent.id`. The child's
        `suggested_universe` narrows to the sidecar's `target_assets` when
        present, else inherits the parent's.

        Sidecar JSON shape (mirroring `recommendations._rec_to_dict`):
        - tune_params: {kind, ..., params: [{param, new_range}, ...], target_assets}
          (legacy single {param, new_range} sidecars still apply)
        - change_exit: {kind, ..., from_exit, to_exit, new_exit_strategy, target_assets}
        - modify_plugins: {kind, ..., ops: [{action, section, slug, params, replaces}],
          target_assets}
        """
        ar = await start_agent_run(self.session, agent_name="translator", strategy_id=parent.id)

        try:
            parent_dir = strategy_dir(parent.slug) / "iteration_001"
            sidecar_path = parent_dir / "analyst_recommendation.json"
            parent_strategy_path = parent_dir / "strategy.json"
            parent_hypothesis_path = parent_dir / "hypothesis.json"

            if not sidecar_path.is_file():
                raise TranslatorError(f"missing analyst_recommendation.json at {sidecar_path}")
            if not parent_strategy_path.is_file():
                raise TranslatorError(f"parent missing strategy.json at {parent_strategy_path}")
            ar.input_artifact_path = str(sidecar_path)

            rec = json.loads(sidecar_path.read_text())
            parent_payload = json.loads(parent_strategy_path.read_text())

            child_payload = json.loads(json.dumps(parent_payload))  # deep copy
            kind = rec.get("kind")
            if kind == "tune_params":
                tunes = rec.get("params")
                if not isinstance(tunes, list):
                    # Legacy pre-M8 single-param sidecar shape.
                    tunes = [{"param": rec.get("param"), "new_range": rec.get("new_range")}]
                grid = child_payload.setdefault("optimization", {}).setdefault("grid_params", {})
                for entry in tunes:
                    param = entry.get("param") if isinstance(entry, dict) else None
                    new_range = entry.get("new_range") if isinstance(entry, dict) else None
                    if not param or not isinstance(new_range, list):
                        raise TranslatorError(f"tune_params entry missing param/new_range: {entry}")
                    grid[param] = new_range
            elif kind == "change_exit":
                new_exit = rec.get("new_exit_strategy")
                if not isinstance(new_exit, dict):
                    raise TranslatorError(
                        "change_exit sidecar missing new_exit_strategy (the Analyst must "
                        "populate it for M4 reiterate; see ChangeExit.new_exit_strategy)"
                    )
                child_payload["exit_strategies"] = [new_exit]
            elif kind == "modify_plugins":
                ops = rec.get("ops")
                if not isinstance(ops, list) or not ops:
                    raise TranslatorError(f"modify_plugins sidecar missing ops: {rec}")
                for op in ops:
                    _apply_plugin_op(child_payload, op)
            else:
                raise TranslatorError(
                    "reiterate only handles tune_params/change_exit/modify_plugins, "
                    f"got kind={kind!r}"
                )

            child_slug = await generate_child_slug(self.session, parent.slug)
            child_payload["name"] = child_slug

            try:
                live = await fetch_live_catalog(self.session, self.fwbg_client)
                validate_strategy_json(
                    child_payload,
                    catalog=live.catalog,
                    presets=live.presets,
                    datasources=live.datasource_names() or None,
                    timeframes=live.timeframes or None,
                )
            except StrategyValidationError as exc:
                raise TranslatorError(str(exc)) from exc

            target_assets = [
                a for a in (rec.get("target_assets") or []) if isinstance(a, str) and a
            ]

            now2 = datetime.now(UTC)
            child = Strategy(
                slug=child_slug,
                current_state=StrategyState.PROPOSED.value,
                iteration_count=1,
                parent_strategy_id=parent.id,
                asset_class=parent.asset_class,
                strategy_family=parent.strategy_family,
                suggested_universe=_child_universe(parent, target_assets),
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
                (child_dir / "hypothesis.json").write_text(json.dumps(hypothesis_data, indent=2))
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

            await finish_agent_run(
                self.session,
                ar,
                status=AgentRunStatus.DONE,
                strategy_id=child.id,
                output_artifact_path=str(child_dir / "strategy.json"),
            )
            await self.session.refresh(child)
            return child
        except Exception as exc:
            await fail_agent_run(self.session, ar, exc)
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
        validate via `validate_strategy_json(..., catalog=<live catalog>)`,
        which rejects any slug not visible in the catalog.

        Parent stays in BACKTESTED; child is a fresh PROPOSED row with
        `parent_strategy_id=parent.id`. Phase-to-field mapping:
            "indicator"          -> indicators
            "feature_selection"  -> feature_selection
            "preprocessing"      -> preprocessing
            "filter"             -> extra_filters
        """
        ar = await start_agent_run(self.session, agent_name="translator", strategy_id=parent.id)

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
                raise TranslatorError(f"parent missing strategy.json at {parent_strategy_path}")
            ar.input_artifact_path = str(sidecar_input_path)

            parent_payload = json.loads(parent_strategy_path.read_text())
            child_payload = json.loads(json.dumps(parent_payload))  # deep copy
            # Inline pipelines get the plugin spliced where fwbg actually runs
            # it. Legacy preset-string pipelines can't be extended in place, so
            # they keep the old top-level list-field (advisory only).
            pipeline = child_payload.get("pipeline")
            if isinstance(pipeline, dict) and list_field in (
                "indicators",
                "preprocessing",
                "feature_selection",
            ):
                pipeline.setdefault(list_field, []).append({"name": plugin_slug, "params": {}})
            else:
                child_payload.setdefault(list_field, []).append(plugin_slug)

            child_slug = await generate_child_slug(self.session, parent.slug)
            child_payload["name"] = child_slug

            live = await fetch_live_catalog(self.session, self.fwbg_client)
            try:
                validate_strategy_json(
                    child_payload,
                    catalog=live.catalog,
                    presets=live.presets,
                    datasources=live.datasource_names() or None,
                    timeframes=live.timeframes or None,
                )
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
                suggested_universe=parent.suggested_universe,
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
            hypothesis_path.write_text(json.dumps(child_hypothesis, indent=2, sort_keys=False))
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

            await finish_agent_run(
                self.session,
                ar,
                status=AgentRunStatus.DONE,
                strategy_id=child.id,
                output_artifact_path=str(strategy_path),
            )
            await self.session.refresh(child)
            return child
        except Exception as exc:
            await fail_agent_run(self.session, ar, exc)
            raise
