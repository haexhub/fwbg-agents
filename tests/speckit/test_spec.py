"""Tests for the speckit Phase-0 foundation: PluginSpec model + renderer +
constitution."""

from __future__ import annotations

import re
from typing import get_args

import pytest
from pydantic import ValidationError

from fwbg_agents.agents.plugin_planner import PluginPhaseLit
from fwbg_agents.orchestrator.plugin_contract import PluginKindLit
from fwbg_agents.speckit import (
    PluginSpec,
    SpecParam,
    load_constitution,
    render_spec_md,
    spec_index_entry,
)


def _valid_spec(**overrides) -> PluginSpec:
    data = {
        "slug": "rsi_extreme_filter",
        "name": "RSI Extreme Filter",
        "kind": "indicator",
        "capability": "Flags bars where RSI is in an overbought/oversold extreme.",
        "summary": "Computes RSI(n) and emits a boolean extreme flag per bar.",
        "inputs": ["close series"],
        "params": [
            SpecParam(name="period", type="int", description="Lookback bars", default=14)
        ],
        "outputs": ["rsi_value", "rsi_extreme_flag"],
        "acceptance_criteria": ["Flag is True only when RSI>70 or RSI<30."],
        "edge_cases": ["All-constant prices yield no extremes."],
    }
    data.update(overrides)
    return PluginSpec(**data)


def test_valid_spec_constructs():
    spec = _valid_spec()
    assert spec.kind == "indicator"
    assert spec.version == "0.1.0"


def test_acceptance_and_edge_cases_are_required():
    with pytest.raises(ValidationError):
        _valid_spec(acceptance_criteria=[])
    with pytest.raises(ValidationError):
        _valid_spec(edge_cases=[])


def test_kind_must_be_canonical():
    with pytest.raises(ValidationError):
        _valid_spec(kind="indicators")  # plural is the phase, not the kind
    with pytest.raises(ValidationError):
        _valid_spec(kind="bogus")


def test_slug_pattern_enforced():
    with pytest.raises(ValidationError):
        _valid_spec(slug="Bad Slug")


def test_capability_length_bounded():
    with pytest.raises(ValidationError):
        _valid_spec(capability="short")  # < 10 chars


def test_capability_must_be_single_line():
    with pytest.raises(ValidationError):
        _valid_spec(capability="Flags RSI extremes.\nAlso computes momentum.")


def test_param_type_must_be_canonical():
    with pytest.raises(ValidationError):
        SpecParam(name="period", type="integer", description="Lookback bars")


def test_index_entry_is_compact():
    entry = spec_index_entry(_valid_spec())
    assert entry == {
        "slug": "rsi_extreme_filter",
        "kind": "indicator",
        "capability": "Flags bars where RSI is in an overbought/oversold extreme.",
    }


def test_render_contains_all_sections():
    md = render_spec_md(_valid_spec())
    for heading in (
        "# Plugin Spec — rsi_extreme_filter",
        "## Capability",
        "## Summary",
        "## Inputs",
        "## Parameters",
        "## Outputs",
        "## Acceptance Criteria",
        "## Edge Cases",
        "## Assumptions",
    ):
        assert heading in md
    assert "AC-001:" in md
    assert "`period` (int, default=14)" in md


def test_render_needs_clarification_marker():
    md = render_spec_md(_valid_spec(needs_clarification=["which RSI period?"]))
    assert "[NEEDS CLARIFICATION: which RSI period?]" in md


def test_constitution_lists_every_canonical_kind_and_phase():
    # Exact set comparison against the backticked names in the two vocabulary
    # bullets of section II — substring checks would let singular kinds pass
    # via their plural phase forms ("indicator" in "indicators").
    text = load_constitution()
    kind_block = text.split("- **kind**", 1)[1].split("- **phase**", 1)[0]
    phase_block = text.split("- **phase**", 1)[1].split("\n\n", 1)[0]
    assert set(re.findall(r"`([a-z_]+)`", kind_block)) == set(get_args(PluginKindLit))
    assert set(re.findall(r"`([a-z_]+)`", phase_block)) == set(
        get_args(PluginPhaseLit)
    )
