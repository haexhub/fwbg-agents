"""Provider dispatch: model_for/model_name_for must route gemini-* names to
GoogleModel and everything else to AnthropicModel, without needing an API key
unless a Gemini model is actually selected."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel

from fwbg_agents.config import settings
from fwbg_agents.tools import llm
from fwbg_agents.tools.llm import (
    AVAILABLE_CLAUDE_MODELS,
    _build_google_model,
    _build_model_for_name,
    list_gemini_models,
)


def test_claude_models_include_default():
    assert "claude-sonnet-5" in AVAILABLE_CLAUDE_MODELS


def test_list_gemini_models_without_key_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_gemini_models_cache", None)
    assert list_gemini_models() == []


def test_list_gemini_models_filters_generate_content_and_strips_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_gemini_models_cache", None)

    class FakeModels:
        def list(self):
            return [
                SimpleNamespace(
                    name="models/gemini-2.5-flash", supported_actions=["generateContent"]
                ),
                SimpleNamespace(
                    name="models/text-embedding-004", supported_actions=["embedContent"]
                ),
                SimpleNamespace(
                    name="models/gemini-2.0-flash", supported_actions=["generateContent"]
                ),
            ]

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(llm.genai, "Client", FakeClient)

    assert list_gemini_models() == ["gemini-2.0-flash", "gemini-2.5-flash"]


def test_list_gemini_models_caches_between_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_gemini_models_cache", None)
    call_count = 0

    class FakeModels:
        def list(self):
            nonlocal call_count
            call_count += 1
            return [
                SimpleNamespace(
                    name="models/gemini-2.5-flash", supported_actions=["generateContent"]
                )
            ]

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(llm.genai, "Client", FakeClient)

    assert list_gemini_models() == ["gemini-2.5-flash"]
    assert list_gemini_models() == ["gemini-2.5-flash"]
    assert call_count == 1


def test_list_gemini_models_falls_back_to_empty_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_gemini_models_cache", None)

    class FailingModels:
        def list(self):
            raise RuntimeError("boom")

    class FailingClient:
        def __init__(self, api_key):
            self.models = FailingModels()

    monkeypatch.setattr(llm.genai, "Client", FailingClient)

    assert list_gemini_models() == []


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
