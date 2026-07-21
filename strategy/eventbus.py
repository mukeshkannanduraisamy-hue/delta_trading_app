"""Thread-safe event bus bridging the trading engine to the WebSocket layer.

The engine runs inside a worker thread (asyncio.to_thread) while WebSocket fan-out
lives on the event loop, so state changes cannot be pushed directly. Producers
call `publish()` from any thread; a single asyncio broadcaster drains the queue
and fans out to subscribers.

Backpressure is explicit: the queue is bounded and drops the OLDEST event when
full (a stale tick matters less than a fresh one) while counting the loss, so an
overwhelmed UI can never stall the trading loop.
"""

from __future__ import annotations

import queue
import time


class EventBus:
    def __init__(self, maxsize: int = 2000) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.published = 0
        self.dropped = 0

    def publish(self, topic: str, data) -> None:
        """Safe to call from ANY thread. Never blocks, never raises."""
        evt = {"topic": topic, "data": data, "ts": time.time()}
        try:
            self._q.put_nowait(evt)
            self.published += 1
        except queue.Full:
            try:                       # drop oldest, keep newest
                self._q.get_nowait()
                self.dropped += 1
                self._q.put_nowait(evt)
                self.published += 1
            except (queue.Empty, queue.Full):
                self.dropped += 1

    def drain(self, max_items: int = 500) -> list[dict]:
        """Called from the event loop; returns everything queued so far."""
        out: list[dict] = []
        while len(out) < max_items:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def stats(self) -> dict:
        return {"published": self.published, "dropped": self.dropped,
                "queued": self._q.qsize()}


bus = EventBus()
