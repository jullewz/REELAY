"""Terminal styling with the Reelay palette (obsidian, graphite, violet)."""

from __future__ import annotations

import os
import shutil
import sys
from typing import TextIO

PALETTE = {
    "accent": "38;2;167;139;250",
    "accent_strong": "1;38;2;196;181;253",
    "muted": "38;2;113;113;122",
    "ok": "38;2;110;231;183",
    "warn": "38;2;251;191;36",
    "err": "38;2;251;113;133",
    "replayed": "38;2;167;139;250",
    "recorded": "38;2;244;114;182",
    "proxied": "38;2;125;211;252",
    "miss": "38;2;251;191;36",
    "chaos": "38;2;251;146;60",
    "error": "38;2;251;113;133",
    "GET": "38;2;125;211;252",
    "POST": "38;2;110;231;183",
    "PUT": "38;2;251;191;36",
    "PATCH": "38;2;251;191;36",
    "DELETE": "38;2;251;113;133",
}


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes  # noqa: PLC0415 (Windows only)
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, AttributeError, ImportError):
        return False


def supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    if not (isatty and isatty()) or os.environ.get("TERM") == "dumb":
        return False
    return _enable_windows_vt()


def supports_unicode(stream: TextIO) -> bool:
    try:
        "─→●✓".encode(getattr(stream, "encoding", None) or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Style:
    def __init__(self, stream: TextIO | None = None, *, color: bool | None = None) -> None:
        stream = stream or sys.stdout
        self.color = supports_color(stream) if color is None else color
        uni = supports_unicode(stream)
        self.arrow = "→" if uni else "->"
        self.dot = "●" if uni else "*"
        self.ok_mark = "✓" if uni else "OK"
        self.rule_char = "─" if uni else "-"

    def __call__(self, text: str, role: str) -> str:
        code = PALETTE.get(role, "")
        return f"\x1b[{code}m{text}\x1b[0m" if self.color and code else text

    def bold(self, text: str) -> str:
        return f"\x1b[1m{text}\x1b[0m" if self.color else text

    def status(self, code: int) -> str:
        role = "ok" if code < 300 else "proxied" if code < 400 else "warn" if code < 500 else "err"
        return self(str(code), role)

    def rule(self, title: str) -> str:
        width = max(50, min(110, shutil.get_terminal_size((100, 24)).columns))
        head = f"{self.rule_char * 2} "
        tail = self.rule_char * max(2, width - len(title) - 4)
        return self(head, "muted") + self(title, "accent_strong") + " " + self(tail, "muted")
