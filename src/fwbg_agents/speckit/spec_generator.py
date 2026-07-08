"""Generate a PluginSpec from an existing plugin's source.

Used by the Phase-1 backfill (documenting the current plugin corpus so the
dedup gate has specs to match against) and reusable by the Phase-3 authoring
reshape. The plugin's identity (slug, kind) is authoritative from where it
lives on disk, so it is coerced onto the generated spec rather than trusted
from the model.
"""

from __future__ import annotations

import logging

from pydantic_ai import Agent
from pydantic_ai.models import Model

from fwbg_agents.orchestrator.plugin_contract import PluginKindLit
from fwbg_agents.speckit import load_constitution
from fwbg_agents.speckit.spec import PluginSpec
from fwbg_agents.tools.llm import default_model

log = logging.getLogger(__name__)

# Plural on-disk plugin directory category → canonical singular PluginKindLit.
# `exit_modifiers` has no PluginKindLit equivalent and is intentionally absent.
CATEGORY_TO_KIND: dict[str, PluginKindLit] = {
    "indicators": "indicator",
    "models": "model",
    "exit_strategies": "exit_strategy",
    "risk_management": "risk_management",
    "entry_modifiers": "entry_modifier",
    "preprocessing": "preprocessing",
    "feature_selection": "feature_selection",
    "data_loading": "data_loading",
}

_INSTRUCTIONS = """
You are documenting an EXISTING fwbg plugin as a structured PluginSpec — you are
describing what the code already does, not designing something new.

Rules:
- `capability`: exactly ONE sentence naming the single capability this plugin
  provides. This is the duplicate-detection anchor, so make it specific and
  discriminating (what it computes/decides), not generic ("an indicator").
- Derive `params` from get_default_params / get_param_schema; `outputs` from
  get_feature_columns / the columns the code actually produces; `inputs` from
  what compute()/transform()/select_features() consumes.
- `acceptance_criteria` and `edge_cases`: state observable behaviour of the
  existing code. Do not invent behaviour that is not in the source.
- If something genuinely cannot be determined from the source, add a short
  `needs_clarification` entry rather than guessing.
- Keep it faithful and concise.
""".strip()


def _coerce_identity(spec: PluginSpec, *, slug: str, kind: PluginKindLit) -> PluginSpec:
    """Force the authoritative on-disk identity onto a generated spec."""
    if spec.slug != slug or spec.kind != kind:
        log.info(
            "spec_generator: coercing identity slug %r->%r kind %r->%r",
            spec.slug, slug, spec.kind, kind,
        )
        return spec.model_copy(update={"slug": slug, "kind": kind})
    return spec


async def generate_spec_from_source(
    *,
    slug: str,
    kind: PluginKindLit,
    source_code: str,
    model: Model | None = None,
) -> PluginSpec:
    """Produce a validated PluginSpec describing the given plugin source."""
    agent: Agent[None, PluginSpec] = Agent(
        model or default_model(),
        output_type=PluginSpec,
        system_prompt=f"{load_constitution()}\n\n{_INSTRUCTIONS}",
    )
    prompt = (
        f"slug: {slug}\n"
        f"kind: {kind}\n\n"
        f"SOURCE:\n```python\n{source_code}\n```\n\n"
        "Emit the PluginSpec for this existing plugin now."
    )
    result = await agent.run(prompt)
    return _coerce_identity(result.output, slug=slug, kind=kind)
