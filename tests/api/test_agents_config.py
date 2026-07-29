"""GET /agents/config: available_models must reflect configured credentials —
Claude always, Gemini only once the "google" secret is actually set."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fwbg_agents.main import app


@pytest_asyncio.fixture
async def config_client(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
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
