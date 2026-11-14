"""Small dependency-free helpers."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

_SIZE_UNITS = {"": 1, "b": 1, "k": 1000, "kb": 1000, "kib": 1024, "m": 1000**2, "mb": 1000**2,
               "mib": 1024**2, "g": 1000**3, "gb": 1000**3, "gib": 1024**3}  # fmt: skip
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def parse_size(value: str | int) -> int:
    """``"10MiB"`` -> bytes."""
    if isinstance(value, bool):
        raise ValueError("size must be a number or a string like '10MiB'")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size must not be negative")
        return value
    match = _SIZE_RE.match(value)
    if not match:
        raise ValueError(f"invalid size {value!r}, expected e.g. '10MiB'")
    number, unit = match.groups()
    factor = _SIZE_UNITS.get(unit.lower())
    if factor is None:
        raise ValueError(f"unknown size unit {unit!r} in {value!r}")
    return int(float(number) * factor)


def parse_duration(value: str | float | int) -> float:
    """``"250ms"``, ``"1.5s"``, ``"2m"`` or a number of seconds -> seconds."""
    if isinstance(value, bool):
        raise ValueError("duration must be a number or a string like '250ms'")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError("duration must not be negative")
        return float(value)
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration {value!r}, expected e.g. '250ms' or '2s'")
    number, unit = float(match.group(1)), match.group(2) or "s"
    return number / 1000 if unit == "ms" else number * 60 if unit == "m" else number


def format_ms(ms: float) -> str:
    if ms < 1:
        return f"{ms:.2f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s"


def format_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover


def new_id() -> str:
    """Sortable 26-character id (ULID layout: 48-bit ms timestamp + 80 random bits)."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def expand_path(value: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Returns 0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def singular(word: str) -> str:
    """Very small English singulariser used for parameter names (``users`` -> ``user``)."""
    lower = word.lower()
    if lower.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if lower.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word
