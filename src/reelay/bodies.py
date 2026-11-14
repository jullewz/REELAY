"""HTTP body codec.

Bodies are stored in the most readable lossless form:

* ``json``   parsed JSON, so tapes stay diffable and hand-editable
* ``text``   decoded text for textual content types
* ``base64`` everything else (images, protobuf, archives)
* ``empty``  no body

Compressed bodies (gzip, deflate) are decoded before storage.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import zlib
from dataclasses import dataclass
from typing import Any

from reelay.errors import TapeError

TEXT_TYPES = ("text/", "application/xml", "application/javascript", "application/x-www-form-urlencoded",
              "application/graphql", "image/svg+xml")  # fmt: skip


def media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def charset(content_type: str | None, default: str = "utf-8") -> str:
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip("\"'")
    return default


def is_json_type(content_type: str | None) -> bool:
    mt = media_type(content_type)
    return mt == "application/json" or mt.endswith("+json")


def decode_content_encoding(data: bytes, encoding: str | None) -> tuple[bytes, bool]:
    """Undo gzip/deflate. Returns ``(data, decoded)``. Unknown encodings are left alone."""
    enc = (encoding or "").strip().lower()
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(data), True
        if enc == "deflate":
            try:
                return zlib.decompress(data), True
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS), True
    except (OSError, zlib.error, EOFError):
        return data, False
    return data, enc in ("", "identity")


@dataclass(frozen=True, slots=True)
class Body:
    encoding: str  # "empty" | "json" | "text" | "base64"
    content: Any = None

    @classmethod
    def empty(cls) -> Body:
        return cls("empty", None)

    @classmethod
    def from_bytes(cls, data: bytes, content_type: str | None) -> Body:
        if not data:
            return cls.empty()
        if is_json_type(content_type):
            try:
                return cls("json", json.loads(data.decode(charset(content_type))))
            except (UnicodeDecodeError, json.JSONDecodeError, LookupError):
                pass
        mt = media_type(content_type)
        if mt.startswith(TEXT_TYPES) or mt == "" or is_json_type(content_type):
            try:
                text = data.decode(charset(content_type))
            except (UnicodeDecodeError, LookupError):
                pass
            else:
                if mt or text.isprintable() or all(c.isprintable() or c in "\r\n\t" for c in text):
                    return cls("text", text)
        return cls("base64", base64.b64encode(data).decode("ascii"))

    def to_bytes(self, content_type: str | None = None) -> bytes:
        if self.encoding == "empty":
            return b""
        if self.encoding == "json":
            return json.dumps(self.content, ensure_ascii=False, separators=(",", ":")).encode(charset(content_type))
        if self.encoding == "text":
            return str(self.content).encode(charset(content_type), errors="replace")
        if self.encoding == "base64":
            try:
                return base64.b64decode(str(self.content), validate=True)
            except (binascii.Error, ValueError):
                raise TapeError("invalid base64 body in tape") from None
        raise TapeError(f"unknown body encoding {self.encoding!r}")

    def to_dict(self) -> dict[str, Any]:
        if self.encoding == "empty":
            return {"encoding": "empty"}
        return {"encoding": self.encoding, "content": self.content}

    @classmethod
    def from_dict(cls, data: Any) -> Body:
        if data is None:
            return cls.empty()
        if not isinstance(data, dict) or data.get("encoding") not in ("empty", "json", "text", "base64"):
            raise TapeError(f"malformed body: {data!r:.80}")
        return cls(data["encoding"], data.get("content"))

    @property
    def size(self) -> int:
        return len(self.to_bytes())

    def preview(self, limit: int = 200) -> str:
        if self.encoding == "empty":
            return ""
        if self.encoding == "json":
            text = json.dumps(self.content, ensure_ascii=False)
        elif self.encoding == "text":
            text = str(self.content)
        else:
            text = f"<{len(self.to_bytes())} bytes binary>"
        return text if len(text) <= limit else text[: limit - 1] + "…"
