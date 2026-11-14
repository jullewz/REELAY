"""Request matching: find the recorded exchange that answers an incoming request.

Every request is reduced to a canonical *key* built from the dimensions in
``MatchSettings.on`` (method, path, query, body, headers), after redaction
and normalisation. Exchanges with identical keys form a queue, served by
the configured strategy:

* ``sequential`` serve recordings in order, then keep serving the last one
  (ideal for polling: ``pending``, ``pending``, ``done``, ``done``...)
* ``cycle``      serve recordings in order and wrap around
* ``first`` / ``last``  always serve the first or last recording

When nothing matches, the closest candidates are scored so a miss can say
*why* it missed: "query differs: page", "body differs at $.user.id".
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

from reelay import jsondiff, jsonpath
from reelay.bodies import media_type
from reelay.models import Exchange, Request, header_get
from reelay.redact import Redactor
from reelay.util import canonical_json

DIMENSIONS = ("method", "path", "query", "body", "headers")
STRATEGIES = ("sequential", "cycle", "first", "last")


@dataclass(slots=True)
class MatchSettings:
    on: tuple[str, ...] = ("method", "path", "query", "body")
    ignore_query: frozenset[str] = frozenset()
    ignore_body: list[str] = field(default_factory=list)
    headers: tuple[str, ...] = ()
    strategy: str = "sequential"
    trailing_slash: bool = False  # True: "/a/" and "/a" are different paths
    fuzzy: bool = False

    def __post_init__(self) -> None:
        unknown = set(self.on) - set(DIMENSIONS)
        if unknown:
            raise ValueError(f"unknown match dimension(s): {', '.join(sorted(unknown))}")
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}, expected one of: {', '.join(STRATEGIES)}")
        for path in self.ignore_body:
            jsonpath.compile_path(path)


@dataclass(frozen=True, slots=True)
class Candidate:
    exchange: Exchange
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchResult:
    exchange: Exchange | None
    kind: str  # "exact" | "fuzzy" | "miss"
    candidates: tuple[Candidate, ...] = ()
    position: int = 0  # index within the queue for this key

    @property
    def hit(self) -> bool:
        return self.exchange is not None


class Matcher:
    def __init__(self, exchanges: Iterable[Exchange] = (), settings: MatchSettings | None = None,
                 redactor: Redactor | None = None) -> None:  # fmt: skip
        self.settings = settings or MatchSettings()
        self.redactor = redactor or Redactor()
        self._lock = threading.Lock()
        self._queues: dict[str, list[Exchange]] = {}
        self._served: dict[str, int] = {}
        self._by_route: dict[tuple[str, str], list[Exchange]] = {}
        self._parts: dict[str, dict[str, Any]] = {}
        for ex in exchanges:
            self.add(ex, redacted=True)

    # -------------------------------------------------------- normalising
    def _path(self, path: str) -> str:
        if not self.settings.trailing_slash and len(path) > 1:
            return path.rstrip("/") or "/"
        return path

    def _body(self, req: Request) -> Any:
        body = req.body
        if body.encoding == "json":
            doc = body.content
            if self.settings.ignore_body:
                doc = copy.deepcopy(doc)
                for path in self.settings.ignore_body:
                    jsonpath.delete(doc, path)
            return ["json", doc]
        if body.encoding == "text":
            text = str(body.content)
            if media_type(req.content_type) == "application/x-www-form-urlencoded":
                return ["form", sorted(parse_qsl(text, keep_blank_values=True))]
            return ["text", text]
        if body.encoding == "base64":
            return ["base64", body.content]
        return ["empty"]

    def parts(self, req: Request, *, redacted: bool = False) -> dict[str, Any]:
        """Normalised value of every dimension for ``req``."""
        if not redacted:
            req = self.redactor.request(req)
        ignored = {q.lower() for q in self.settings.ignore_query}
        return {
            "method": req.method.upper(),
            "path": self._path(req.path),
            "query": sorted((k, v) for k, v in req.query if k.lower() not in ignored),
            "body": self._body(req),
            "headers": [[h.lower(), header_get(req.headers, h) or ""] for h in self.settings.headers],
        }

    def key(self, req: Request, *, redacted: bool = False) -> str:
        parts = self.parts(req, redacted=redacted)
        return canonical_json({d: parts[d] for d in self.settings.on})

    # ------------------------------------------------------------ indexing
    def add(self, exchange: Exchange, *, redacted: bool = True) -> None:
        parts = self.parts(exchange.request, redacted=redacted)
        key = canonical_json({d: parts[d] for d in self.settings.on})
        with self._lock:
            self._queues.setdefault(key, []).append(exchange)
            self._by_route.setdefault((parts["method"], parts["path"]), []).append(exchange)
            self._parts[exchange.id] = parts

    def reset(self) -> None:
        with self._lock:
            self._served.clear()

    def __len__(self) -> int:
        return sum(len(q) for q in self._queues.values())

    # ------------------------------------------------------------- lookup
    def _pick(self, key: str) -> tuple[Exchange, int]:
        queue = self._queues[key]
        strategy = self.settings.strategy
        if strategy == "first":
            return queue[0], 0
        if strategy == "last":
            return queue[-1], len(queue) - 1
        served = self._served.get(key, 0)
        self._served[key] = served + 1
        index = served % len(queue) if strategy == "cycle" else min(served, len(queue) - 1)
        return queue[index], index

    def find(self, req: Request) -> MatchResult:
        parts = self.parts(req)
        key = canonical_json({d: parts[d] for d in self.settings.on})
        with self._lock:
            if key in self._queues:
                exchange, index = self._pick(key)
                return MatchResult(exchange, "exact", position=index)
            candidates = self._candidates(parts)
        if self.settings.fuzzy and candidates:
            return MatchResult(candidates[0].exchange, "fuzzy", tuple(candidates[:5]))
        return MatchResult(None, "miss", tuple(candidates[:5]))

    def _candidates(self, parts: dict[str, Any]) -> list[Candidate]:
        pool = self._by_route.get((parts["method"], parts["path"]))
        same_route = pool is not None
        if pool is None:  # fall back to same path with any method, then everything
            pool = [ex for (m, p), exs in self._by_route.items() if p == parts["path"] for ex in exs]
            if not pool:
                pool = [ex for exs in self._by_route.values() for ex in exs][:200]
        scored: list[Candidate] = []
        for ex in pool:
            other = self._parts[ex.id]
            reasons: list[str] = []
            score = 0
            for dim in self.settings.on:
                if other[dim] == parts[dim]:
                    score += 1
                else:
                    reasons.append(self._explain(dim, other[dim], parts[dim]))
            if same_route:
                score += 2
            scored.append(Candidate(ex, score, tuple(reasons)))
        scored.sort(key=lambda c: -c.score)
        return scored

    @staticmethod
    def _explain(dim: str, recorded: Any, incoming: Any) -> str:
        if dim == "query":
            rec, inc = dict(recorded), dict(incoming)
            names = sorted({k for k in rec.keys() | inc.keys() if rec.get(k) != inc.get(k)})
            return "query differs: " + ", ".join(names[:6])
        if dim == "body" and recorded[0] == incoming[0] == "json":
            deltas = jsondiff.diff(recorded[1], incoming[1], max_deltas=3)
            return "body differs at " + ", ".join(d.path for d in deltas)
        if dim == "headers":
            names = [r[0] for r, i in zip(recorded, incoming, strict=False) if r != i]
            return "header differs: " + ", ".join(names)
        if dim == "path":
            return f"path differs: recorded {recorded}"
        if dim == "method":
            return f"method differs: recorded {recorded}"
        return f"{dim} differs"
