"""Append-only trade journal (JSONL on disk + in-memory ring for the UI)."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import config


class Journal:
    def __init__(self, maxlen: int = 500) -> None:
        self.events: deque = deque(maxlen=maxlen)
        self.path = config.JOURNAL_DIR / "trades.jsonl"
        config.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        # record() runs on the engine worker thread while recent() serves API
        # requests from the event loop — guard the deque against concurrent
        # mutation-during-iteration.
        self._lock = threading.Lock()

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def record(self, kind: str, data: dict) -> dict:
        event = {"ts": time.time(), "iso": self._stamp(), "kind": kind, **data}
        with self._lock:
            self.events.appendleft(event)
        # Push to any live dashboards immediately — no polling delay.
        try:
            from .eventbus import bus as _bus
            _bus.publish("journal", event)
        except Exception:  # noqa: BLE001 — never let the UI break the engine
            pass
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except Exception as exc:  # noqa: BLE001 — journaling must never crash the engine
            print(f"[journal] write failed: {exc!r}")
        return event

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self.events)[:limit]


journal = Journal()
