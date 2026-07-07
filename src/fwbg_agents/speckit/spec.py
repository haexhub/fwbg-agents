"""PluginSpec — the structured "what" artifact for a plugin/indicator.

Adapted from spec-kit's `spec.md`: this is the WHAT (capability, interface,
acceptance criteria), separate from the HOW (the PluginPlan / plan.md). It
replaces the old free-form ``spec_md: str`` (min_length=80, never validated,
never read) with a validated model + a markdown renderer.

The ``capability`` line is the duplicate-detection anchor: a new capability is
matched against the ``capability`` of every existing plugin spec before a new
plugin is authored (see the plugin-constitution and the dedup gate).

``kind`` reuses the canonical ``PluginKindLit`` from ``plugin_contract`` so the
spec, the contract, and the catalog share one vocabulary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fwbg_agents.orchestrator.plugin_contract import PluginKindLit

SPEC_FILENAME = "spec.md"


class SpecParam(BaseModel):
    """One configurable parameter, at spec (interface) altitude — the rich
    min/max/step/choices metadata lives in the plan/contract, not here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    default: int | float | bool | str | list | None = None


class PluginSpec(BaseModel):
    """Structured specification for a single plugin/indicator."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    kind: PluginKindLit
    # One-line capability statement — the dedup anchor. Kept short on purpose.
    capability: str = Field(min_length=10, max_length=200)
    summary: str = Field(min_length=1)
    inputs: list[str] = []
    params: list[SpecParam] = []
    outputs: list[str] = []
    acceptance_criteria: list[str] = Field(min_length=1)
    edge_cases: list[str] = Field(min_length=1)
    assumptions: list[str] = []
    needs_clarification: list[str] = []
    version: str = Field(default="0.1.0", min_length=1)


def spec_index_entry(spec: PluginSpec) -> dict[str, str]:
    """Compact entry for the dedup index: what a matcher needs to decide
    "do we already have this?" without loading the full spec."""
    return {"slug": spec.slug, "kind": spec.kind, "capability": spec.capability}


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- _none_"


def render_spec_md(spec: PluginSpec) -> str:
    """Render a PluginSpec to spec-kit-flavored markdown (the on-disk spec.md)."""
    params = (
        "\n".join(
            f"- `{p.name}` ({p.type}, default={p.default!r}): {p.description}"
            for p in spec.params
        )
        if spec.params
        else "- _none_"
    )
    acceptance = "\n".join(
        f"- AC-{i:03d}: {c}" for i, c in enumerate(spec.acceptance_criteria, 1)
    )
    sections = [
        f"# Plugin Spec — {spec.slug}",
        "",
        f"**Kind**: {spec.kind}  •  **Version**: {spec.version}",
        "",
        "## Capability",
        "",
        spec.capability,
        "",
        "## Summary",
        "",
        spec.summary,
        "",
        "## Inputs",
        "",
        _bullets(spec.inputs),
        "",
        "## Parameters",
        "",
        params,
        "",
        "## Outputs",
        "",
        _bullets(spec.outputs),
        "",
        "## Acceptance Criteria",
        "",
        acceptance,
        "",
        "## Edge Cases",
        "",
        _bullets(spec.edge_cases),
        "",
        "## Assumptions",
        "",
        _bullets(spec.assumptions),
    ]
    if spec.needs_clarification:
        sections += [
            "",
            "## Needs Clarification",
            "",
            "\n".join(f"- [NEEDS CLARIFICATION: {c}]" for c in spec.needs_clarification),
        ]
    return "\n".join(sections) + "\n"
