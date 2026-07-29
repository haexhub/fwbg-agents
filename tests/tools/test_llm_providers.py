"""Provider dispatch: model_for/model_name_for must route gemini-* names to
GoogleModel and everything else to AnthropicModel, without needing an API key
unless a Gemini model is actually selected."""

from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel

from fwbg_agents.config import settings
from fwbg_agents.tools.llm import (
    AVAILABLE_GEMINI_MODELS,
    AVAILABLE_MODELS,
    _build_google_model,
    _build_model_for_name,
)


def test_available_models_is_claude_plus_gemini():
    assert set(AVAILABLE_GEMINI_MODELS) <= set(AVAILABLE_MODELS)
    assert "claude-sonnet-5" in AVAILABLE_MODELS
    assert "gemini-2.5-pro" in AVAILABLE_MODELS


def test_dispatch_routes_claude_name_to_anthropic_model():
    model = _build_model_for_name("claude-sonnet-5")
    assert isinstance(model, AnthropicModel)


def test_dispatch_routes_gemini_name_to_google_model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    model = _build_model_for_name("gemini-2.5-pro")
    assert isinstance(model, GoogleModel)
    assert model.model_name == "gemini-2.5-pro"


def test_build_google_model_applies_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    model = _build_google_model("gemini-2.5-flash")
    assert model.settings is not None
    assert model.settings.get("timeout") == settings.llm_timeout_seconds


def test_build_google_model_without_api_key_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Gemini API key"):
        _build_google_model("gemini-2.5-flash")
