"""Search-provider package (M4b): Tavily + Brave behind a common protocol,
with a fallback client trying providers in order."""

from __future__ import annotations

from fwbg_agents.tools.search.base import SearchProvider, SearchResult, SearchUnavailableError
from fwbg_agents.tools.search.brave import BraveClient
from fwbg_agents.tools.search.fallback import FallbackSearchClient
from fwbg_agents.tools.search.tavily import TavilyClient

__all__ = [
    "BraveClient",
    "FallbackSearchClient",
    "SearchProvider",
    "SearchResult",
    "SearchUnavailableError",
    "TavilyClient",
]
