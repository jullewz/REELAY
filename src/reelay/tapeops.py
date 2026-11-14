"""Tape operations: statistics, filtering, merging and re-redaction."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from reelay.matching import Matcher, MatchSettings
from reelay.models import Exchange
from reelay.openapi import template_paths
from reelay.redact import Redactor
from reelay.routes import compile_route
from reelay.util import percentile


@dataclass(frozen=True, slots=True)
class EndpointStats:
    method: str
    path: str
    count: int
    statuses: dict[int, int]
    p50_ms: float
    p95_ms: float
    avg_size: int

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "path": self.path, "count": self.count,
                "statuses": {str(k): v for k, v in self.statuses.items()}, "p50_ms": round(self.p50_ms, 2),
                "p95_ms": round(self.p95_ms, 2), "avg_size": self.avg_size}  # fmt: skip


def endpoint_stats(exchanges: Iterable[Exchange]) -> list[EndpointStats]:
    exs = list(exchanges)
    mapping = template_paths(e.request.path for e in exs)
    groups: dict[tuple[str, str], list[Exchange]] = defaultdict(list)
    for ex in exs:
        groups[(ex.request.method, mapping[ex.request.path])].append(ex)
    out = []
    for (method, path), items in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        statuses: dict[int, int] = defaultdict(int)
        for ex in items:
            statuses[ex.response.status] += 1
        durations = [ex.duration_ms for ex in items]
        sizes = [ex.response.body.size for ex in items]
        out.append(EndpointStats(method, path, len(items), dict(sorted(statuses.items())),
                                 percentile(durations, 50), percentile(durations, 95),
                                 sum(sizes) // max(1, len(sizes))))  # fmt: skip
    return out


def filter_exchanges(exchanges: Iterable[Exchange], *, include: Iterable[str] = (), exclude: Iterable[str] = (),
                     status: Iterable[int] = ()) -> list[Exchange]:  # fmt: skip
    inc = [compile_route(p) for p in include]
    exc = [compile_route(p) for p in exclude]
    statuses = set(status)
    out = []
    for ex in exchanges:
        m, p = ex.request.method, ex.request.path
        if inc and not any(r.matches(m, p) for r in inc):
            continue
        if any(r.matches(m, p) for r in exc):
            continue
        if statuses and ex.response.status not in statuses:
            continue
        out.append(ex)
    return out


def merge(tapes: Iterable[Iterable[Exchange]], *, dedupe: bool = False,
          settings: MatchSettings | None = None) -> list[Exchange]:  # fmt: skip
    """Concatenate tapes. With ``dedupe``, keep only the last exchange per match key."""
    combined = [ex for tape in tapes for ex in tape]
    if not dedupe:
        return combined
    matcher = Matcher(settings=settings or MatchSettings(), redactor=Redactor())
    last: dict[str, int] = {}
    for index, ex in enumerate(combined):
        last[matcher.key(ex.request, redacted=True)] = index
    keep = set(last.values())
    return [ex for index, ex in enumerate(combined) if index in keep]


def redact_all(exchanges: Iterable[Exchange], redactor: Redactor) -> tuple[list[Exchange], int]:
    """Re-apply redaction. Returns the new exchanges and how many changed."""
    out, changed = [], 0
    for ex in exchanges:
        clean = redactor.exchange(ex)
        if clean.to_dict() != ex.to_dict():
            changed += 1
        out.append(clean)
    return out, changed
