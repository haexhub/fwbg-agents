"""GET /agents/config: available_models must reflect configured credentials —
Claude always, Gemini only once the "google" secret is actually set."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fwbg_agents.main import app


@pytest_asyncio.fixture
async def config_client(tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.tools import llm

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Claude models are listed live from haex-claude-proxy — stub it out so
    # tests don't make a real network call.
    monkeypatch.setattr(llm, "list_claude_models", lambda: ["claude-sonnet-5"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_gemini_models_hidden_without_google_key(config_client):
    resp = await config_client.get("/agents/config")
    assert resp.status_code == 200
    models = resp.json()["available_models"]
    assert "claude-sonnet-5" in models
    assert "gemini-2.5-pro" not in models


async def test_gemini_models_shown_once_google_key_set(config_client, monkeypatch):
    from fwbg_agents.tools import llm

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "list_gemini_models", lambda: ["gemini-2.5-pro"])
    resp = await config_client.get("/agents/config")
    assert resp.status_code == 200
    models = resp.json()["available_models"]
    assert "claude-sonnet-5" in models
    assert "gemini-2.5-pro" in models


async def test_selecting_a_model_probes_it_before_storing(config_client, monkeypatch):
    from fwbg_agents.tools import agent_config, llm

    probed: list[str] = []
    monkeypatch.setattr(llm, "probe_model", probed.append)

    resp = await config_client.put("/agents/config/researcher", json={"model": "claude-sonnet-5"})
    assert resp.status_code == 200
    assert probed == ["claude-sonnet-5"]
    assert agent_config.get_model_override("researcher") == "claude-sonnet-5"


async def test_unusable_model_is_rejected_with_the_provider_message(config_client, monkeypatch):
    """A model the provider refuses must surface *why* and leave the override
    untouched — the operator has to be able to pick a different one."""
    from fwbg_agents.tools import agent_config, llm

    def refuse(model: str) -> None:
        raise llm.ModelUnusableError(
            "status_code: 429, body: {'error': {'message': 'Quota exceeded ... limit: 0'}}"
        )

    monkeypatch.setattr(llm, "probe_model", refuse)

    resp = await config_client.put("/agents/config/researcher", json={"model": "claude-sonnet-5"})
    assert resp.status_code == 422
    assert "limit: 0" in resp.json()["detail"]
    assert agent_config.get_model_override("researcher") is None


async def test_resetting_to_the_default_model_skips_the_probe(config_client, monkeypatch):
    """Clearing an override needs no provider call — there is nothing to test."""
    from fwbg_agents.tools import llm

    def fail(model: str) -> None:
        raise AssertionError(f"probe_model must not run for a reset, got {model!r}")

    monkeypatch.setattr(llm, "probe_model", fail)

    resp = await config_client.put("/agents/config/researcher", json={"model": ""})
    assert resp.status_code == 200
