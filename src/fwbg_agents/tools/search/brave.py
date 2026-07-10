"""Brave Search client — secondary search provider (M4b)."""

from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.tools.search.base import SearchResult, SearchUnavailableError
from fwbg_agents.tools.search.tavily import _log_quota

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_TIMEOUT_SECONDS = 30.0


class BraveClient:
    """Async Brave Search API client."""

    name = "brave"

    def __init__(
        self,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        """Initialize."""
        self.api_key = api_key
        self.base_url = base_url
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created internally."""
        if self._owns_http:
            await self._http.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        session: AsyncSession | None = None,
        agent_run_id: int | None = None,
    ) -> list[SearchResult]:
        """Execute a Brave web search and return up to max_results results."""
        if not self.api_key:
            raise SearchUnavailableError("BRAVE_API_KEY is not set")

        start = time.monotonic()
        response = await self._http.get(
            self.base_url,
            params={"q": query, "count": max_results},
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
            },
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        for raw in data.get("web", {}).get("results", []):
            try:
                results.append(
                    SearchResult(
                        url=raw["url"],
                        title=raw["title"],
                        content_snippet=raw["description"],
                        # Brave's API has no per-result relevance score
                        # (unlike Tavily's `score`) — fixed 1.0 is a real
                        # API limitation, not an oversight.
                        score=1.0,
                    )
                )
            except (KeyError, TypeError) as exc:
                log.warning("brave: skipping malformed result %s: %s", raw, exc)
                continue

        if session is not None and agent_run_id is not None:
            await _log_quota(session, agent_run_id, self.name, elapsed_ms)

        return results
