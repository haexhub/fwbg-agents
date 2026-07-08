"""The LLM model factory must own the Anthropic client's timeout + retry budget.

Regression guard for the ~6min stacked-timeout failures: a too-short 120s
per-request timeout combined with the SDK's default 3 attempts guillotined
every long Opus generation.
"""

from __future__ import annotations

from fwbg_agents.config import settings
from fwbg_agents.tools.llm import _build_model


def test_defaults_allow_long_generations_with_bounded_retries():
    # 600s matches Anthropic's non-streaming ceiling; a small retry budget
    # bounds a wedged-proxy hang instead of stacking full-timeout attempts.
    assert settings.llm_timeout_seconds >= 600.0
    assert settings.llm_max_retries <= 2


def test_build_model_applies_timeout_and_retries_to_client():
    model = _build_model("claude-opus-4-8")
    assert model.client.max_retries == settings.llm_max_retries
    assert float(model.client.timeout) == settings.llm_timeout_seconds
