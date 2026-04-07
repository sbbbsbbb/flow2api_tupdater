"""Dashboard event bus for SSE updates."""
import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict


def _encode_sse(event: str, data: Dict[str, Any], event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


class DashboardEventBus:
    """Lightweight in-memory broadcaster for dashboard SSE."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._sequence = 0

    async def publish(self, event_type: str, payload: Dict[str, Any] | None = None) -> None:
        self._sequence += 1
        message = {
            "id": self._sequence,
            "type": event_type,
            "payload": payload or {},
            "timestamp": time.time(),
        }

        stale_queues = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                stale_queues.append(queue)

        for queue in stale_queues:
            self._subscribers.discard(queue)

    async def stream(self, *, heartbeat_seconds: int = 20) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)

        try:
            yield _encode_sse(
                "ready",
                {
                    "type": "ready",
                    "timestamp": time.time(),
                },
            )

            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except asyncio.TimeoutError:
                    yield _encode_sse(
                        "heartbeat",
                        {
                            "type": "heartbeat",
                            "timestamp": time.time(),
                        },
                    )
                    continue

                yield _encode_sse(
                    "dashboard",
                    message,
                    event_id=message["id"],
                )
        finally:
            self._subscribers.discard(queue)


dashboard_events = DashboardEventBus()
