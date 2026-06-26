"""FallbackSearchClient — tries providers in a fixed order (M4b, decision B).

No scoring/merging of results across providers. If every provider fails,
returns `[]` so the Researcher continues without sources rather than
failing the whole run — the same graceful-degradation behavior Tavily-only
callers already relied on pre-M4b.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.tools.search.base import SearchProvider, SearchResult, SearchUnavailableError

log = logging.getLogger(__name__)


class FallbackSearchClient:
    name = "fallback"

    def __init__(self, providers: list[SearchProvider]):
        self.providers = providers

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        session: AsyncSession | None = None,
        agent_run_id: int | None = None,
    ) -> list[SearchResult]:
        for provider in self.providers:
            try:
                results = await provider.search(
                    query,
                    max_results=max_results,
                    session=session,
                    agent_run_id=agent_run_id,
                )
            except (SearchUnavailableError, httpx.HTTPStatusError) as exc:
                log.warning(
                    "fallback search: %s unavailable (%s), trying next provider",
                    provider.name,
                    exc,
                )
                continue
            log.info("fallback search: query served by %s", provider.name)
            return results

        log.warning(
            "fallback search: all %d provider(s) failed for query=%r",
            len(self.providers),
            query,
        )
        return []
