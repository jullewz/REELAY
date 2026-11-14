"""In-memory traffic log and publish/subscribe bus for the live dashboard."""

from __future__ import annotations

import contextlib
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from reelay.models import Exchange, Request, Response
from reelay.util import new_id, percentile, utc_now


@dataclass(slots=True)
class TrafficEvent:
    """One request handled by the server, whatever its source."""

    id: str
    at: str
    source: str  # "recorded" | "replayed" | "proxied" | "miss" | "chaos" | "error"
    method: str
    target: str
    status: int
    duration_ms: float
    match: str | None = None
    chaos: str | None = None
    note: str | None = None
    exchange: Exchange | None = field(default=None, repr=False)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "at": self.at, "source": self.source, "method": self.method, "target": self.target,
                "status": self.status, "duration_ms": round(self.duration_ms, 2), "match": self.match,
                "chaos": self.chaos, "note": self.note}  # fmt: skip

    def detail(self) -> dict[str, Any]:
        data = self.summary()
        if self.exchange is not None:
            data["exchange"] = self.exchange.to_dict()
        return data


def make_event(source: str, req: Request, resp: Response, duration_ms: float, **kw: Any) -> TrafficEvent:
    exchange = kw.pop("exchange", None) or Exchange(req, resp, duration_ms=duration_ms)
    return TrafficEvent(new_id(), utc_now(), source, req.method, req.target, resp.status, duration_ms,
                        exchange=exchange, **kw)  # fmt: skip


class EventBus:
    def __init__(self, capacity: int = 1000) -> None:
        self._events: deque[TrafficEvent] = deque(maxlen=capacity)
        self._subscribers: set[queue.Queue[TrafficEvent | None]] = set()
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}

    def publish(self, event: TrafficEvent) -> None:
        with self._lock:
            self._events.append(event)
            self.counts[event.source] = self.counts.get(event.source, 0) + 1
            subscribers = list(self._subscribers)
        for sub in subscribers:
            with contextlib.suppress(queue.Full):  # a slow dashboard must never slow the proxy down
                sub.put_nowait(event)

    def subscribe(self) -> queue.Queue[TrafficEvent | None]:
        q: queue.Queue[TrafficEvent | None] = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[TrafficEvent | None]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            with contextlib.suppress(queue.Full):
                sub.put_nowait(None)

    def recent(self, limit: int = 200) -> list[TrafficEvent]:
        with self._lock:
            return list(self._events)[-limit:][::-1]

    def get(self, event_id: str) -> TrafficEvent | None:
        with self._lock:
            return next((e for e in self._events if e.id == event_id), None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self.counts.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            counts = dict(self.counts)
        durations = [e.duration_ms for e in events]
        return {
            "total": sum(counts.values()),
            "by_source": counts,
            "latency_ms": {
                "p50": round(percentile(durations, 50), 2),
                "p95": round(percentile(durations, 95), 2),
                "p99": round(percentile(durations, 99), 2),
            },
            "errors": sum(1 for e in events if e.status >= 500),
        }
