"""speckit — spec-driven authoring for plugins/indicators.

Adapts the GitHub spec-kit methodology (constitution → spec → plan) to the
autonomous plugin pipeline. Phase 0 provides the foundation: the structured
``PluginSpec`` artifact + renderer and the plugin constitution.
"""

from __future__ import annotations

from pathlib import Path

from fwbg_agents.speckit.spec import (
    SPEC_FILENAME,
    PluginSpec,
    SpecParam,
    render_spec_md,
    spec_index_entry,
)

CONSTITUTION_PATH = Path(__file__).parents[3] / "prompts" / "plugin_constitution.md"


def load_constitution() -> str:
    """Return the plugin-constitution markdown (the MUST-rules every plugin obeys)."""
    return CONSTITUTION_PATH.read_text(encoding="utf-8")


__all__ = [
    "CONSTITUTION_PATH",
    "SPEC_FILENAME",
    "PluginSpec",
    "SpecParam",
    "load_constitution",
    "render_spec_md",
    "spec_index_entry",
]
