"""BraveClient tests (M4b)."""

from __future__ import annotations

import httpx
import pytest

from fwbg_agents.tools.search import BraveClient, SearchResult, SearchUnavailableError


def _mock_transport(payload, status=200):
    async def handler(request):
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_brave_search_happy_path():
    payload = {
        "web": {
            "results": [
                {"url": "https://x", "title": "X", "description": "snippet"},
                {"url": "https://y", "title": "Y", "description": "other"},
            ]
        }
    }
    http = httpx.AsyncClient(transport=_mock_transport(payload))
    client = BraveClient(api_key="k", http=http)
    results = await client.search("query")
    assert results == [
        SearchResult(url="https://x", title="X", content_snippet="snippet", score=1.0),
        SearchResult(url="https://y", title="Y", content_snippet="other", score=1.0),
    ]


@pytest.mark.asyncio
async def test_brave_raises_search_unavailable_without_key():
    client = BraveClient(api_key=None)
    with pytest.raises(SearchUnavailableError):
        await client.search("q")
