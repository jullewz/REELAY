"""Chaos injection: latency, errors, dropped connections and bandwidth limits.

Rules are matched by route (first match wins). A seed makes every run
reproducible, which matters when a flaky test needs to be debugged.

Latency syntax:

* ``"250ms"``            fixed
* ``"100ms..400ms"``     uniform range
* ``"normal(200ms,50ms)"`` normal distribution, clamped at zero
"""

from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass, field

from reelay.routes import compile_route
from reelay.util import parse_duration, parse_size

_RANGE = re.compile(r"^\s*([^.]+?)\s*\.\.\s*(.+?)\s*$")
_NORMAL = re.compile(r"^\s*normal\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*$", re.IGNORECASE)
_BANDWIDTH = re.compile(r"^\s*(.+?)\s*/\s*s\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Latency:
    kind: str  # "fixed" | "uniform" | "normal"
    a: float
    b: float = 0.0

    @classmethod
    def parse(cls, spec: str | float | int) -> Latency:
        if isinstance(spec, int | float) and not isinstance(spec, bool):
            return cls("fixed", float(spec))
        text = str(spec)
        if m := _NORMAL.match(text):
            return cls("normal", parse_duration(m.group(1)), parse_duration(m.group(2)))
        if m := _RANGE.match(text):
            lo, hi = parse_duration(m.group(1)), parse_duration(m.group(2))
            if hi < lo:
                raise ValueError(f"latency range {text!r} has max below min")
            return cls("uniform", lo, hi)
        return cls("fixed", parse_duration(text))

    def sample(self, rng: random.Random) -> float:
        if self.kind == "uniform":
            return rng.uniform(self.a, self.b)
        if self.kind == "normal":
            return max(0.0, rng.gauss(self.a, self.b))
        return self.a


def parse_bandwidth(spec: str) -> int:
    """``"64KiB/s"`` -> bytes per second."""
    m = _BANDWIDTH.match(spec)
    if not m:
        raise ValueError(f"invalid bandwidth {spec!r}, expected e.g. '64KiB/s'")
    value = parse_size(m.group(1))
    if value <= 0:
        raise ValueError("bandwidth must be positive")
    return value


@dataclass(frozen=True, slots=True)
class ChaosRule:
    route: str = "/**"
    latency: Latency | None = None
    error_rate: float = 0.0
    error_status: int = 503
    drop_rate: float = 0.0
    bandwidth: int | None = None  # bytes per second

    def __post_init__(self) -> None:
        compile_route(self.route)
        for name in ("error_rate", "drop_rate"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
        if not 400 <= self.error_status <= 599:
            raise ValueError("error_status must be a 4xx or 5xx code")


@dataclass(frozen=True, slots=True)
class ChaosDecision:
    delay: float = 0.0
    error_status: int | None = None
    drop: bool = False
    bandwidth: int | None = None
    rule: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.delay or self.error_status or self.drop or self.bandwidth)

    def describe(self) -> str:
        parts = []
        if self.delay:
            parts.append(f"+{self.delay * 1000:.0f}ms")
        if self.error_status:
            parts.append(f"error {self.error_status}")
        if self.drop:
            parts.append("dropped")
        if self.bandwidth:
            parts.append(f"{self.bandwidth // 1024} KiB/s")
        return ", ".join(parts)


@dataclass(slots=True)
class ChaosEngine:
    rules: list[ChaosRule] = field(default_factory=list)
    seed: int | None = None
    enabled: bool = True
    _rng: random.Random = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def decide(self, method: str, path: str) -> ChaosDecision:
        if not self.enabled:
            return ChaosDecision()
        for rule in self.rules:
            if not compile_route(rule.route).matches(method, path):
                continue
            with self._lock:
                drop = rule.drop_rate > 0 and self._rng.random() < rule.drop_rate
                error = not drop and rule.error_rate > 0 and self._rng.random() < rule.error_rate
                delay = rule.latency.sample(self._rng) if rule.latency else 0.0
            return ChaosDecision(delay, rule.error_status if error else None, drop, rule.bandwidth, rule.route)
        return ChaosDecision()
