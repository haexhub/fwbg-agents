"""Tests for the fwbg HTTP client wrapper.

Uses httpx.MockTransport (built-in, no extra dep) to simulate fwbg responses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fwbg_agents.tools.fwbg_client import FwbgClient, FwbgClientError


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://fwbg-test")


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
