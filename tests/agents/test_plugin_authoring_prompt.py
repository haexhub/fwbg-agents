"""M5d: smoke-tests for the canonical fwbg-Plugin-Konventionen prompt-doc.

Asserts that the doc exists at the canonical path, all required headings are
present, and that the documented PluginPhase enum values match the agents-side
expectations. fwbg-agents intentionally does not import fwbg_sdk (decoupled via
HTTP/filesystem), so the expected phase names are pinned here. Update both this
list and the prompt-doc when the SDK adds/renames a phase.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "plugin_authoring.md"

REQUIRED_HEADINGS = [
    "BasePlugin Contract",
    "PluginPhase Enum",
    "Phase-Specific Subclasses",
    "Parameters: `get_default_params` + `get_param_schema`",
    "Feature Columns: `get_feature_columns`",
    "Tests Convention (`tests.py`)",
    "File Layout",
    "Worked Examples",
]

# Pinned to the SDK enum as of 2026-06-25. Update when fwbg_sdk.base.PluginPhase changes.
EXPECTED_PHASE_NAMES = {
    "DATA_LOADING",
    "PREPROCESSING",
    "INDICATORS",
    "FEATURE_SELECTION",
    "EXIT_STRATEGIES",
    "RISK_MANAGEMENT",
    "LABELING",
    "MODEL",
    "VALIDATION",
}


def test_prompt_file_exists_at_canonical_path() -> None:
    assert PROMPT_PATH.is_file(), f"expected canonical prompt-doc at {PROMPT_PATH}"


def test_prompt_file_is_non_trivial() -> None:
    body = PROMPT_PATH.read_text(encoding="utf-8")
    assert len(body) > 2000, "prompt-doc seems too short — likely missing sections"


@pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
def test_required_heading_present(heading: str) -> None:
    body = PROMPT_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)
    assert pattern.search(body), f"missing required heading: ## {heading}"


def test_all_expected_phases_documented() -> None:
    """Every expected PluginPhase name must appear in the prompt-doc."""
    body = PROMPT_PATH.read_text(encoding="utf-8")
    missing = {name for name in EXPECTED_PHASE_NAMES if name not in body}
    assert not missing, f"prompt-doc missing PluginPhase names: {sorted(missing)}"


def test_no_unknown_phases_mentioned() -> None:
    """If the doc mentions a PluginPhase.X that isn't in the pinned set, flag it."""
    body = PROMPT_PATH.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"\bPluginPhase\.([A-Z_]+)\b", body))
    extras = mentioned - EXPECTED_PHASE_NAMES
    assert not extras, f"prompt-doc mentions unknown PluginPhase names: {sorted(extras)}"
