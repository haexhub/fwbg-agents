"""Tests for GET/PUT /agents/secrets."""

from __future__ import annotations

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fwbg_agents.main import app


@pytest_asyncio.fixture
async def secrets_client(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_get_secrets_returns_all_known_keys_unset(secrets_client, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    resp = await secrets_client.get("/agents/secrets")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert keys["tavily"] == {"set": False}
    assert keys["brave"] == {"set": False}


async def test_get_secrets_reflects_env_fallback(secrets_client, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv-from-env")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    resp = await secrets_client.get("/agents/secrets")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert keys["tavily"]["set"] is True
    assert keys["brave"]["set"] is False


async def test_put_secrets_stores_key(secrets_client, tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    resp = await secrets_client.put("/agents/secrets", json={"tavily": "tv-abc123"})
    assert resp.status_code == 200
    assert resp.json()["keys"]["tavily"]["set"] is True

    secrets_file = tmp_path / "secrets.json"
    assert secrets_file.is_file()
    data = json.loads(secrets_file.read_text())
    assert data["tavily"] == "tv-abc123"


async def test_put_secrets_clears_key(secrets_client, tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.tools.secrets import set_secret

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    set_secret("tavily", "tv-abc123")

    # Clear by sending null
    resp = await secrets_client.put("/agents/secrets", json={"tavily": None})
    assert resp.status_code == 200
    assert resp.json()["keys"]["tavily"]["set"] is False

    data = json.loads((tmp_path / "secrets.json").read_text())
    assert "tavily" not in data


async def test_put_secrets_does_not_return_values(secrets_client, tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    resp = await secrets_client.put("/agents/secrets", json={"brave": "br-secret-key"})
    body = resp.json()
    # Verify no key value leaks into the response
    assert "br-secret-key" not in json.dumps(body)
    assert body["keys"]["brave"] == {"set": True}


async def test_get_secret_function_reads_file_over_env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.tools import secrets as sec_mod

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "env-value")
    sec_mod.set_secret("tavily", "file-value")

    assert sec_mod.get_secret("tavily") == "file-value"


async def test_get_secret_function_falls_back_to_env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.tools import secrets as sec_mod

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("BRAVE_API_KEY", "env-brave")

    assert sec_mod.get_secret("brave") == "env-brave"


async def test_get_secret_function_returns_none_when_unset(tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.tools import secrets as sec_mod

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    assert sec_mod.get_secret("brave") is None
