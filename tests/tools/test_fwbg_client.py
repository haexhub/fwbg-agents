"""Tests for the fwbg HTTP client wrapper.

Uses httpx.MockTransport (built-in, no extra dep) to simulate fwbg responses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fwbg_agents.tools.fwbg_client import FwbgClient, FwbgClientError, safe_fwbg_strategy_name


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://fwbg-test")


def test_safe_fwbg_strategy_name_appends_iteration_suffix():
    assert safe_fwbg_strategy_name("orb__forex__001", 1) == "orb__forex__001__it001"


def test_safe_fwbg_strategy_name_keeps_existing_iteration_suffix():
    # child slugs already carry __itNNN — no second suffix
    assert safe_fwbg_strategy_name("orb__forex__001__it002", 1) == "orb__forex__001__it002"


async def test_default_client_sends_api_key_header():
    client = FwbgClient(base_url="http://fwbg-test", api_key="secret")
    assert client._http.headers.get("X-API-Key") == "secret"
    await client.aclose()


async def test_default_client_omits_api_key_header_when_none():
    client = FwbgClient(base_url="http://fwbg-test", api_key=None)
    assert "X-API-Key" not in client._http.headers
    await client.aclose()


async def test_injected_http_client_is_used_as_is():
    # An externally supplied client must not be mutated with an API key.
    http = _mock_client(lambda request: httpx.Response(200, json={}))
    client = FwbgClient(base_url="http://fwbg-test", http=http, api_key="secret")
    assert client._http is http
    assert "X-API-Key" not in http.headers
    await http.aclose()


async def test_start_run_posts_strategy_name_and_returns_job():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "job_id": "20260624_120000_abcdef",
                "status": "running",
                "strategy_name": "demo_v1",
                "pid": 4242,
            },
        )

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    job = await client.start_run("demo_v1", asset_classes=["FOREX"])

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/runs/start")
    assert captured["body"] == {"strategy_name": "demo_v1", "asset_classes": ["FOREX"]}
    assert job["job_id"] == "20260624_120000_abcdef"
    assert job["status"] == "running"
    await http.aclose()


async def test_start_run_omits_asset_classes_when_none():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"job_id": "x", "status": "running"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    await client.start_run("demo_v1")

    assert captured["body"] == {"strategy_name": "demo_v1"}
    await http.aclose()


async def test_get_progress_returns_parsed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/runs/job_42/progress"
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "job_id": "job_42",
                "status": "running",
                "progress": 0.55,
                "phase": "walk_forward",
            },
        )

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    prog = await client.get_progress("job_42")

    assert prog["status"] == "running"
    assert prog["progress"] == 0.55
    await http.aclose()


async def test_get_run_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/runs/job_42"
        return httpx.Response(
            200,
            json={
                "run_id": "job_42",
                "status": "completed",
                "assets": {
                    "EURUSD": {
                        "symbol": "EURUSD",
                        "status": "completed",
                        "unified_metrics": {"sharpe": 1.8, "profit_factor": 1.7},
                    }
                },
            },
        )

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    run = await client.get_run("job_42")

    assert run["status"] == "completed"
    assert run["assets"]["EURUSD"]["unified_metrics"]["sharpe"] == 1.8
    await http.aclose()


async def test_get_plugin_source_returns_source():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/plugins/fwbg-core.indicators.adx/source"
        return httpx.Response(
            200,
            json={
                "fqn": "fwbg-core.indicators.adx",
                "filename": "__init__.py",
                "source": "class Adx(BaseIndicator): ...",
            },
        )

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    out = await client.get_plugin_source("fwbg-core.indicators.adx")

    assert out["filename"] == "__init__.py"
    assert out["source"].startswith("class Adx")
    await http.aclose()


async def test_get_plugin_source_404_raises_fwbg_client_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Plugin not found: nope"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(FwbgClientError) as exc:
        await client.get_plugin_source("nope")
    assert "404" in str(exc.value)
    await http.aclose()


async def test_non_200_raises_fwbg_client_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Run not found"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(FwbgClientError) as exc:
        await client.get_run("missing")
    assert "404" in str(exc.value)
    assert "Run not found" in str(exc.value)
    await http.aclose()


async def test_default_constructor_makes_its_own_client(monkeypatch):
    """When no http is passed, FwbgClient creates one and closes it on aclose()."""
    client = FwbgClient(base_url="http://fwbg-test")
    assert client._http is not None
    await client.aclose()


# ---------------------------------------------------------------------------
# Transient-transport-error retry on idempotent GETs.
#
# Observed live: uvicorn's 5s keep-alive timeout races the Runner's 5s poll
# interval; a single dropped connection (ReadError / RemoteProtocolError)
# used to abort a backtest that was still running fine on the fwbg side.
# ---------------------------------------------------------------------------


async def test_get_retries_transient_transport_errors(monkeypatch):
    from fwbg_agents.tools import fwbg_client as mod

    monkeypatch.setattr(mod, "_GET_RETRY_BACKOFF_SECONDS", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ReadError("server dropped keep-alive", request=request)
        return httpx.Response(200, json={"status": "running"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    progress = await client.get_progress("job_1")

    assert progress == {"status": "running"}
    assert attempts["n"] == 3
    await http.aclose()


async def test_get_gives_up_after_max_retries(monkeypatch):
    from fwbg_agents.tools import fwbg_client as mod

    monkeypatch.setattr(mod, "_GET_RETRY_BACKOFF_SECONDS", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response.", request=request
        )

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(httpx.RemoteProtocolError):
        await client.get_progress("job_1")

    assert attempts["n"] == 3
    await http.aclose()


async def test_get_does_not_retry_http_error_statuses():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, text="not found")

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(FwbgClientError):
        await client.get_progress("job_1")

    assert attempts["n"] == 1  # non-2xx is a real answer, not a transport blip
    await http.aclose()


async def test_post_is_not_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadError("boom", request=request)

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(httpx.ReadError):
        await client.create_strategy("s1", {"name": "s1"})

    assert attempts["n"] == 1  # POSTs are not idempotent — never auto-retry
    await http.aclose()


# ---------------------------------------------------------------------------
# Research register: run-start fields, card endpoints, start timeout.
# ---------------------------------------------------------------------------


async def test_start_run_sends_research_id_and_note():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"job_id": "x", "status": "starting"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    await client.start_run("demo_v1", research_id="S-0001", note="initial backtest: demo")

    assert captured["body"] == {
        "strategy_name": "demo_v1",
        "research_id": "S-0001",
        "note": "initial backtest: demo",
    }
    await http.aclose()


async def test_start_run_sends_reason_and_holdout_when_given():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"job_id": "x", "status": "starting"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    await client.start_run(
        "demo_v1",
        research_id="S-0001",
        note="promote gate holdout look",
        reason="promote gate: further holdout look (demo)",
        holdout=True,
    )

    assert captured["body"]["reason"] == "promote gate: further holdout look (demo)"
    assert captured["body"]["holdout"] is True
    await http.aclose()


async def test_start_run_omits_reason_when_none_and_holdout_when_false():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"job_id": "x", "status": "starting"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    await client.start_run("demo_v1", research_id="S-0001", note="n", reason=None, holdout=False)

    assert "reason" not in captured["body"]
    assert "holdout" not in captured["body"]
    await http.aclose()


async def test_start_run_uses_long_timeout_for_the_preflight():
    timeouts: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts[request.url.path] = request.extensions["timeout"]
        if request.url.path == "/api/runs/start":
            return httpx.Response(200, json={"job_id": "job_1", "status": "starting"})
        return httpx.Response(200, json={"run_id": "job_1", "status": "running"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    await client.start_run("demo_v1", research_id="S-0001", note="n")
    await client.get_run("job_1")
    await client.create_strategy("s1", {"name": "s1"})

    assert timeouts["/api/runs/start"]["read"] == 150.0
    assert timeouts["/api/runs/start"]["write"] == 150.0
    default_read = http.timeout.read
    assert default_read != 150.0
    assert timeouts["/api/runs/job_1"]["read"] == default_read
    assert timeouts["/api/strategies"]["read"] == default_read
    await http.aclose()


async def test_start_run_refusal_raises_fwbg_client_error():
    refusal = {
        "detail": {
            "message": "research gate refused the run",
            "denials": [{"code": "after_cutoff", "status": 409, "message": "ends after cutoff"}],
        }
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=refusal)

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(FwbgClientError) as exc:
        await client.start_run("demo_v1", research_id="S-0001", note="n")
    assert exc.value.status == 409
    assert json.loads(exc.value.body) == refusal
    await http.aclose()


async def test_create_research_card_posts_card_fields_and_returns_json():
    captured: dict = {}
    response = {"card": {"id": "S-0042", "title": "Demo idea"}, "warning": "not committed"}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=response)

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    out = await client.create_research_card(
        title="Demo idea",
        tags=["forex", "breakout"],
        hypothesis="Opening ranges break with momentum.",
        mechanism="Overnight order flow accumulates at the open.",
        cutoff="2026-04-22",
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/research"
    assert captured["body"] == {
        "title": "Demo idea",
        "tags": ["forex", "breakout"],
        "hypothesis": "Opening ranges break with momentum.",
        "mechanism": "Overnight order flow accumulates at the open.",
        "cutoff": "2026-04-22",
    }
    assert out == response
    await http.aclose()


async def test_create_research_card_non_2xx_raises_fwbg_client_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "tags must not be empty"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(FwbgClientError) as exc:
        await client.create_research_card(
            title="t", tags=[], hypothesis="h", mechanism="m", cutoff="2026-04-22"
        )
    assert exc.value.status == 422
    await http.aclose()


async def test_get_research_card_returns_card_and_trials():
    response = {
        "card": {"id": "S-0042"},
        "trials": [{"kind": "iteration"}, {"kind": "holdout"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/research/S-0042"
        return httpx.Response(200, json=response)

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    out = await client.get_research_card("S-0042")

    assert out == response
    await http.aclose()


async def test_get_research_card_non_2xx_raises_fwbg_client_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "card not found"})

    http = _mock_client(handler)
    client = FwbgClient(base_url="http://fwbg-test", http=http)

    with pytest.raises(FwbgClientError) as exc:
        await client.get_research_card("S-9999")
    assert exc.value.status == 404
    await http.aclose()
