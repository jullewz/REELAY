"""Tape storage.

A tape is a JSON Lines file. The first line is a header, each following
line is one exchange::

    {"reelay_tape": 1, "name": "github", "created_at": "...", "upstream": "https://api.github.com"}
    {"id": "01J...", "request": {...}, "response": {...}, ...}

JSON Lines keeps tapes append-only (safe while recording), easy to diff in
code review and easy to edit by hand. A truncated last line, left by a
crash mid-write, is skipped with a warning instead of failing the load.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reelay.errors import TapeError
from reelay.models import Exchange
from reelay.util import utc_now

log = logging.getLogger(__name__)

TAPE_VERSION = 1


@dataclass(slots=True)
class TapeHeader:
    name: str
    created_at: str = field(default_factory=utc_now)
    upstream: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"reelay_tape": TAPE_VERSION, "name": self.name, "created_at": self.created_at,
                                "upstream": self.upstream}  # fmt: skip
        if self.meta:
            data["meta"] = self.meta
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TapeHeader:
        version = data.get("reelay_tape")
        if version != TAPE_VERSION:
            raise TapeError(f"unsupported tape version {version!r} (this Reelay reads version {TAPE_VERSION})")
        return cls(str(data.get("name") or "tape"), str(data.get("created_at") or utc_now()), data.get("upstream"),
                   dict(data.get("meta") or {}))  # fmt: skip


class Tape:
    """An ordered, thread-safe collection of exchanges backed by a file."""

    def __init__(self, path: Path | None, header: TapeHeader, exchanges: Iterable[Exchange] = ()) -> None:
        self.path = path
        self.header = header
        self._exchanges: list[Exchange] = list(exchanges)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ creation
    @classmethod
    def new(cls, path: Path | None, name: str | None = None, upstream: str | None = None) -> Tape:
        return cls(path, TapeHeader(name or (path.stem if path else "tape"), upstream=upstream))

    @classmethod
    def load(cls, path: Path) -> Tape:
        try:
            handle = path.open("r", encoding="utf-8")
        except FileNotFoundError:
            raise TapeError(f"tape not found: {path}", hint="record one with `reelay record`") from None
        except OSError as exc:
            raise TapeError(f"cannot read tape {path}: {exc}") from None
        with handle:
            lines = handle.read().splitlines()
        if not lines or not lines[0].strip():
            raise TapeError(f"{path} is empty, not a Reelay tape")
        try:
            header = TapeHeader.from_dict(json.loads(lines[0]))
        except (json.JSONDecodeError, AttributeError):
            raise TapeError(f"{path} does not start with a Reelay tape header") from None
        exchanges: list[Exchange] = []
        for number, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue
            try:
                exchanges.append(Exchange.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                if number == len(lines):
                    log.warning("%s: ignoring truncated last line %d (interrupted write?)", path, number)
                    continue
                raise TapeError(f"{path}:{number}: invalid JSON") from None
            except TapeError as exc:
                raise TapeError(f"{path}:{number}: {exc.message}") from None
        return cls(path, header, exchanges)

    @classmethod
    def open_or_create(cls, path: Path, upstream: str | None = None) -> Tape:
        if path.exists() and path.stat().st_size > 0:
            tape = cls.load(path)
            if upstream and not tape.header.upstream:
                tape.header.upstream = upstream
            return tape
        tape = cls.new(path, upstream=upstream)
        tape.save()
        return tape

    # -------------------------------------------------------------- access
    def __len__(self) -> int:
        return len(self._exchanges)

    def __iter__(self) -> Iterator[Exchange]:
        with self._lock:
            return iter(list(self._exchanges))

    @property
    def exchanges(self) -> list[Exchange]:
        with self._lock:
            return list(self._exchanges)

    def get(self, exchange_id: str) -> Exchange:
        with self._lock:
            matches = [e for e in self._exchanges if e.id == exchange_id or e.id.startswith(exchange_id)]
        if not matches:
            raise TapeError(f"no exchange with id {exchange_id!r}")
        if len(matches) > 1:
            raise TapeError(f"id prefix {exchange_id!r} is ambiguous ({len(matches)} matches)")
        return matches[0]

    # ------------------------------------------------------------ mutation
    def append(self, exchange: Exchange, *, persist: bool = True) -> None:
        """Add an exchange. With a backing file, the line is appended and flushed immediately."""
        line = json.dumps(exchange.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            self._exchanges.append(exchange)
            if persist and self.path is not None:
                if not self.path.exists():
                    self._write(self._exchanges[:-1])
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()

    def replace_all(self, exchanges: Iterable[Exchange]) -> None:
        with self._lock:
            self._exchanges = list(exchanges)

    def save(self, path: Path | None = None) -> Path:
        """Atomically rewrite the whole tape (temp file + rename)."""
        target = path or self.path
        if target is None:
            raise TapeError("this tape has no file path")
        with self._lock:
            if path is not None:
                self.path = path
            self._write(self._exchanges, target)
        return target

    def _write(self, exchanges: list[Exchange], target: Path | None = None) -> None:
        dest = target or self.path
        assert dest is not None
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(self.header.to_dict(), ensure_ascii=False) + "\n")
                for ex in exchanges:
                    handle.write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
