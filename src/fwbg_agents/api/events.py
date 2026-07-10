"""SSE event stream — real pub/sub via fwbg_agents.events."""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from fwbg_agents import events as event_bus

router = APIRouter(prefix="/events", tags=["events"])


async def _stream() -> AsyncIterator[dict[str, str]]:
    """Yield SSE-formatted dicts from the event bus subscription."""
    async for event in event_bus.subscribe():
        yield {"data": json.dumps(event)}


@router.get("/stream")
async def event_stream() -> EventSourceResponse:
    """Stream server-sent events from the internal event bus."""
    return EventSourceResponse(_stream())
