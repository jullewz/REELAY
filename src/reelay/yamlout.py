"""Minimal YAML emitter for JSON-compatible data (enough for OpenAPI documents).

Strings are quoted whenever plain style could be misread (``yes``, ``1.0``,
``null``, leading symbols, ``: `` inside), so the output always parses back
to the same data with any YAML 1.2 parser.
"""

from __future__ import annotations

import json
import re
from typing import Any

_PLAIN = re.compile(r"^[A-Za-z_/][\w ./{}()-]*$")
_RESERVED = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    text = str(value)
    if text and _PLAIN.match(text) and text.lower() not in _RESERVED and not text.endswith(" ") and ": " not in text:
        return text
    return json.dumps(text, ensure_ascii=False)


def _key(key: Any) -> str:
    return _scalar(str(key))


def dump(data: Any) -> str:
    lines: list[str] = []

    def emit(node: Any, indent: int) -> None:
        pad = "  " * indent
        if isinstance(node, dict):
            if not node:
                lines.append(f"{pad}{{}}")
                return
            for key, value in node.items():
                if (isinstance(value, dict) and value) or (isinstance(value, list) and value):
                    lines.append(f"{pad}{_key(key)}:")
                    emit(value, indent + 1)
                else:
                    empty = "{}" if isinstance(value, dict) else "[]" if isinstance(value, list) else _scalar(value)
                    lines.append(f"{pad}{_key(key)}: {empty}")
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and item:
                    first = True
                    for key, value in item.items():
                        prefix = f"{pad}- " if first else f"{pad}  "
                        first = False
                        if isinstance(value, dict | list) and value:
                            lines.append(f"{prefix}{_key(key)}:")
                            emit(value, indent + 2)
                        else:
                            empty = (
                                "{}" if isinstance(value, dict) else "[]" if isinstance(value, list) else _scalar(value)
                            )
                            lines.append(f"{prefix}{_key(key)}: {empty}")
                elif isinstance(item, list) and item:
                    lines.append(f"{pad}-")
                    emit(item, indent + 1)
                else:
                    empty = "{}" if isinstance(item, dict) else "[]" if isinstance(item, list) else _scalar(item)
                    lines.append(f"{pad}- {empty}")
        else:
            lines.append(f"{pad}{_scalar(node)}")

    emit(data, 0)
    return "\n".join(lines) + "\n"
