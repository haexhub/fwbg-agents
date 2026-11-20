"""Phase 3.2: verify that evaluate_plugin ships VERIFIED plugins to fwbg.

Tests focus on _register_verified_plugin_in_fwbg, which is the new surface area:
- correct payload is sent via FwbgClient.register_plugin
- fwbg errors are swallowed (best-effort semantics)
- missing plugin.py is handled gracefully
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from fwbg_agents.orchestrator.plugin_flow import (
    _register_verified_plugin_in_fwbg,
)
from fwbg_agents.persistence.models import Plugin, PluginState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plugin(tmp_path, slug="my_indicator", kind="indicator", *, code=True, spec=True):
    plugin_dir = tmp_path / "plugins" / slug / "v1"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    if code:
        (plugin_dir / "plugin.py").write_text("# stub plugin code\n", encoding="utf-8")

    spec_path = None
    if spec:
        sp = plugin_dir / "spec.md"
        sp.write_text("# Spec\ncapability: rolling mean of close\n", encoding="utf-8")
        spec_path = str(sp)

    p = Plugin(
        slug=slug,
        current_state=PluginState.VERIFIED.value,
        kind=kind,
        spec_path=spec_path,
        contract_path=str(plugin_dir / "contract.yaml"),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    p.id = 1
    return p


def _mock_client():
    """Return a mock FwbgClient with register_plugin + aclose as AsyncMocks."""
    inst = AsyncMock()
    inst.register_plugin = AsyncMock(
        return_value={
            "fqn": "agent-authored:my_indicator",
            "slug": "my_indicator",
            "category": "indicators",
        }
    )
    inst.aclose = AsyncMock()
    return inst


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_sends_slug_code_kind_spec(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    plugin = _plugin(tmp_path)
    client = _mock_client()

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        await _register_verified_plugin_in_fwbg(plugin)

    client.register_plugin.assert_awaited_once()
    kwargs = client.register_plugin.call_args.kwargs
    assert kwargs["slug"] == "my_indicator"
    assert kwargs["python_code"] == "# stub plugin code\n"
    assert kwargs["kind"] == "indicator"
    assert "capability: rolling mean of close" in kwargs["spec_md"]
    assert kwargs["overwrite"] is True


@pytest.mark.asyncio
async def test_register_closes_client_on_success(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    plugin = _plugin(tmp_path)
    client = _mock_client()

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        await _register_verified_plugin_in_fwbg(plugin)

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_with_no_spec_sends_empty_spec_md(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    plugin = _plugin(tmp_path, spec=False)
    client = _mock_client()

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        await _register_verified_plugin_in_fwbg(plugin)

    kwargs = client.register_plugin.call_args.kwargs
    assert kwargs["spec_md"] == ""


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fwbg_error_is_swallowed_not_raised(tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.tools.fwbg_client import FwbgClientError

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    plugin = _plugin(tmp_path)
    client = _mock_client()
    client.register_plugin = AsyncMock(side_effect=FwbgClientError(422, "bad code"))

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        # Must not raise — best-effort semantics
        await _register_verified_plugin_in_fwbg(plugin)

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_network_error_is_swallowed_not_raised(tmp_path, monkeypatch):
    import httpx

    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    plugin = _plugin(tmp_path)
    client = _mock_client()
    client.register_plugin = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        await _register_verified_plugin_in_fwbg(plugin)

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_plugin_py_skips_registration(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    plugin = _plugin(tmp_path, code=False)  # no plugin.py written
    client = _mock_client()

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        await _register_verified_plugin_in_fwbg(plugin)

    client.register_plugin.assert_not_awaited()
    # Client is not even created in this path, so no aclose to check


# ---------------------------------------------------------------------------
# FwbgClient.register_plugin shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fwbg_client_register_plugin_posts_correct_body():
    """Unit-test the HTTP layer: register_plugin calls POST /api/plugins."""
    import httpx

    from fwbg_agents.tools.fwbg_client import FwbgClient

    posted: list[dict] = []

    async def _fake_post(request: httpx.Request) -> httpx.Response:
        import json

        posted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "fqn": "agent-authored:my_indicator",
                "slug": "my_indicator",
                "category": "indicators",
            },
        )

    transport = httpx.MockTransport(_fake_post)
    http = httpx.AsyncClient(base_url="http://fwbg", transport=transport)
    client = FwbgClient(base_url="http://fwbg", http=http)

    await client.register_plugin(
        slug="my_indicator",
        python_code="# code",
        kind="indicator",
        spec_md="# Spec",
        overwrite=True,
    )
    await client.aclose()

    assert len(posted) == 1
    body = posted[0]
    assert body["slug"] == "my_indicator"
    assert body["kind"] == "indicator"
    assert body["overwrite"] is True
    assert body["spec_md"] == "# Spec"


@pytest.mark.asyncio
async def test_register_plugin_403_names_the_upload_env_var():
    """fwbg's upload API is opt-in; the 403 must say which flag to set."""
    import httpx

    from fwbg_agents.tools.fwbg_client import (
        FwbgClient,
        FwbgClientError,
        PluginUploadDisabledError,
    )

    async def _forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": "Plugin registration is disabled on this server. "
                "Set FWBG_ALLOW_PLUGIN_UPLOAD=1 to enable it."
            },
        )

    http = httpx.AsyncClient(base_url="http://fwbg", transport=httpx.MockTransport(_forbidden))
    client = FwbgClient(base_url="http://fwbg", http=http)
    with pytest.raises(PluginUploadDisabledError) as info:
        await client.register_plugin(slug="x", python_code="# code", kind="indicator")
    await client.aclose()

    assert isinstance(info.value, FwbgClientError)
    assert info.value.status == 403
    assert "FWBG_ALLOW_PLUGIN_UPLOAD=1" in str(info.value)
    assert "fwbg backend" in str(info.value)


@pytest.mark.asyncio
async def test_register_plugin_other_403_is_not_reported_as_upload_disabled():
    import httpx

    from fwbg_agents.tools.fwbg_client import (
        FwbgClient,
        FwbgClientError,
        PluginUploadDisabledError,
    )

    async def _forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Path traversal not allowed"})

    http = httpx.AsyncClient(base_url="http://fwbg", transport=httpx.MockTransport(_forbidden))
    client = FwbgClient(base_url="http://fwbg", http=http)
    with pytest.raises(FwbgClientError) as info:
        await client.register_plugin(slug="x", python_code="# code", kind="indicator")
    await client.aclose()

    assert not isinstance(info.value, PluginUploadDisabledError)


@pytest.mark.asyncio
async def test_upload_disabled_message_reaches_run_event(tmp_path, monkeypatch):
    """The flow stays best-effort but the emitted failure names the env var."""
    from fwbg_agents.config import settings
    from fwbg_agents.tools.fwbg_client import PluginUploadDisabledError

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    plugin = _plugin(tmp_path)
    client = _mock_client()
    client.register_plugin = AsyncMock(
        side_effect=PluginUploadDisabledError("Set FWBG_ALLOW_PLUGIN_UPLOAD=1")
    )

    with (
        patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client),
        patch("fwbg_agents.orchestrator.plugin_flow.emit_run_event") as emit,
    ):
        await _register_verified_plugin_in_fwbg(plugin, agent_run_id=7)

    emit.assert_called_once()
    assert emit.call_args.args[:2] == (7, "plugin_registration_failed")
    assert "FWBG_ALLOW_PLUGIN_UPLOAD=1" in emit.call_args.kwargs["error"]
