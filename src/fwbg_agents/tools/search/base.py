"""Search-provider protocol shared by Tavily/Brave (M4b).

`SearchProvider` is structural (no common base class needed) so
`FallbackSearchClient` can wrap any provider that exposes `name` + `search()`.
`SearchUnavailableError` generalizes the M4 `TavilyUnavailableError` to any
provider whose API key/config is missing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


class SearchResult(BaseModel):
    url: str
    title: str
    content_snippet: str
    score: float


class SearchUnavailableError(RuntimeError):
    """Raised when a search provider is not configured (e.g. missing API key)."""


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        session: AsyncSession | None = None,
        agent_run_id: int | None = None,
    ) -> list[SearchResult]: ...
