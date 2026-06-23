"""Mock SSE event stream. Will be replaced by real orchestrator events in M2+."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/events", tags=["events"])


async def _mock_events() -> AsyncIterator[dict[str, str]]:
    """Emit a heartbeat event every 5 seconds until the client disconnects."""
    counter = 0
    while True:
        counter += 1
        payload = {
            "type": "heartbeat",
            "counter": counter,
            "ts": datetime.now(UTC).isoformat(),
        }
        yield {"event": "heartbeat", "data": json.dumps(payload)}
        await asyncio.sleep(5)


@router.get("/stream")
async def event_stream() -> EventSourceResponse:
    return EventSourceResponse(_mock_events())
