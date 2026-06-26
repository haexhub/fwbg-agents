"""SearchProvider protocol conformance (M4b)."""

from __future__ import annotations

from fwbg_agents.tools.search import SearchProvider, TavilyClient


def test_tavily_client_satisfies_search_provider_protocol():
    client = TavilyClient(api_key="k")
    assert isinstance(client, SearchProvider)
