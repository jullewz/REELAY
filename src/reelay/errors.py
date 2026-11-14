"""Exception hierarchy. Each error maps to a process exit code."""

from __future__ import annotations

EXIT_OK = 0
EXIT_DIFFERENCES = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 4


class ReelayError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = EXIT_INTERNAL

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(ReelayError):
    exit_code = EXIT_USAGE


class ConfigError(ReelayError):
    exit_code = EXIT_USAGE


class TapeError(ReelayError):
    """A tape file is missing, unreadable or malformed."""

    exit_code = EXIT_USAGE


class UpstreamError(ReelayError):
    """The upstream server could not be reached or answered badly."""

    exit_code = EXIT_INTERNAL
