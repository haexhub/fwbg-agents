"""In-process asyncio event bus for SSE broadcasting.

Each SSE subscriber gets its own asyncio.Queue. `emit()` is fire-and-forget
and safe to call from any async context in the same event loop (e.g. from
inside a tool callback). Slow consumers are silently dropped (QueueFull).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

_subscribers: set[asyncio.Queue[dict]] = set()

_HEARTBEAT_INTERVAL = 5.0
_QUEUE_MAXSIZE = 200


def emit(event: dict) -> None:
    """Broadcast event to all connected SSE subscribers."""
    if "ts" not in event:
        event = {**event, "ts": datetime.now(UTC).isoformat()}
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def subscribe() -> AsyncIterator[dict]:
    """Async generator that yields events; yields a heartbeat every 5 s if idle."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _subscribers.add(q)
    try:
        while True:
            try:
                yield await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                yield {"type": "heartbeat", "ts": datetime.now(UTC).isoformat()}
    finally:
        _subscribers.discard(q)
