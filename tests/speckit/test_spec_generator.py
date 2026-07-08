"""Tests for the deterministic parts of the spec generator (the LLM call itself
is exercised by the backfill run, not unit-tested)."""

from __future__ import annotations

from typing import get_args

from fwbg_agents.orchestrator.plugin_contract import PluginKindLit
from fwbg_agents.speckit.spec import PluginSpec
from fwbg_agents.speckit.spec_generator import CATEGORY_TO_KIND, _coerce_identity


def _spec(**overrides) -> PluginSpec:
    data = {
        "slug": "wrong",
        "name": "X",
        "kind": "model",
        "capability": "Computes something specific per bar.",
        "summary": "s",
        "acceptance_criteria": ["a"],
        "edge_cases": ["e"],
    }
    data.update(overrides)
    return PluginSpec(**data)


def test_category_to_kind_values_are_canonical():
    valid = set(get_args(PluginKindLit))
    assert set(CATEGORY_TO_KIND.values()) <= valid


def test_coerce_identity_overrides_slug_and_kind():
    coerced = _coerce_identity(_spec(), slug="ema", kind="indicator")
    assert coerced.slug == "ema"
    assert coerced.kind == "indicator"
    # untouched fields survive the copy
    assert coerced.capability == "Computes something specific per bar."


def test_coerce_identity_noop_when_already_correct():
    spec = _spec(slug="ema", kind="indicator")
    assert _coerce_identity(spec, slug="ema", kind="indicator") is spec
