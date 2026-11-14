"""Secret redaction.

Tapes end up in git repositories and CI logs, so Reelay removes credentials
*before* anything is written to disk. The same redaction runs on incoming
requests during replay, so a request carrying a real token still matches the
recorded request that carries ``[REDACTED]``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode

from reelay import jsonpath
from reelay.bodies import Body, media_type
from reelay.models import Exchange, Headers, Request, Response

PLACEHOLDER = "[REDACTED]"

DEFAULT_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key",
    "x-auth-token", "x-csrf-token", "x-xsrf-token", "x-amz-security-token", "x-goog-api-key",
})  # fmt: skip

DEFAULT_PARAMS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey", "key", "secret",
    "client_secret", "password", "passwd", "signature", "sig", "auth", "code",
})  # fmt: skip

DEFAULT_JSON_KEYS = frozenset({
    "password", "passwd", "secret", "token", "access_token", "refresh_token", "id_token",
    "client_secret", "api_key", "apikey", "private_key", "authorization", "session", "otp",
})  # fmt: skip

DEFAULT_PATTERNS = (
    r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",  # JWT
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key id
    r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",  # GitHub token
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",  # Slack token
    r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b",  # Stripe secret key
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
)


@dataclass(slots=True)
class RedactionRules:
    enabled: bool = True
    headers: frozenset[str] = DEFAULT_HEADERS
    query_params: frozenset[str] = DEFAULT_PARAMS
    json_keys: frozenset[str] = DEFAULT_JSON_KEYS
    json_paths: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    placeholder: str = PLACEHOLDER

    @classmethod
    def build(
        cls,
        *,
        enabled: bool = True,
        extra_headers: Iterable[str] = (),
        extra_params: Iterable[str] = (),
        extra_json_keys: Iterable[str] = (),
        json_paths: Iterable[str] = (),
        extra_patterns: Iterable[str] = (),
        keep_defaults: bool = True,
        placeholder: str = PLACEHOLDER,
    ) -> RedactionRules:
        base_h = DEFAULT_HEADERS if keep_defaults else frozenset()
        base_p = DEFAULT_PARAMS if keep_defaults else frozenset()
        base_j = DEFAULT_JSON_KEYS if keep_defaults else frozenset()
        patterns = (list(DEFAULT_PATTERNS) if keep_defaults else []) + list(extra_patterns)
        for pattern in patterns:
            re.compile(pattern)  # validate early
        paths = list(json_paths)
        for path in paths:
            jsonpath.compile_path(path)
        return cls(
            enabled=enabled,
            headers=base_h | {h.lower() for h in extra_headers},
            query_params=base_p | {p.lower() for p in extra_params},
            json_keys=base_j | {k.lower() for k in extra_json_keys},
            json_paths=paths,
            patterns=patterns,
            placeholder=placeholder,
        )


class Redactor:
    def __init__(self, rules: RedactionRules | None = None) -> None:
        self.rules = rules or RedactionRules()
        self._patterns = [re.compile(p) for p in self.rules.patterns]

    # ---------------------------------------------------------------- pieces
    def text(self, value: str) -> str:
        for pattern in self._patterns:
            value = pattern.sub(self.rules.placeholder, value)
        return value

    def headers(self, headers: Headers) -> Headers:
        out: Headers = []
        for name, value in headers:
            if name.lower() in self.rules.headers:
                out.append((name, self.rules.placeholder))
            else:
                out.append((name, self.text(value)))
        return out

    def params(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(k, self.rules.placeholder if k.lower() in self.rules.query_params else self.text(v)) for k, v in pairs]

    def json(self, document: Any) -> Any:
        doc = copy.deepcopy(document)
        placeholder = self.rules.placeholder
        keys = self.rules.json_keys

        def walk(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: placeholder if k.lower() in keys and v is not None else walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v) for v in node]
            if isinstance(node, str):
                return self.text(node)
            return node

        doc = walk(doc)
        for path in self.rules.json_paths:
            if path.strip() == "$":
                return placeholder
            jsonpath.assign(doc, path, placeholder)
        return doc

    def body(self, body: Body, content_type: str | None) -> Body:
        if body.encoding == "json":
            return Body("json", self.json(body.content))
        if body.encoding == "text":
            text = str(body.content)
            if media_type(content_type) == "application/x-www-form-urlencoded":
                pairs = parse_qsl(text, keep_blank_values=True)
                if pairs:
                    return Body("text", urlencode(self.params(pairs)))
            return Body("text", self.text(text))
        return body

    # ------------------------------------------------------------- messages
    def request(self, req: Request) -> Request:
        if not self.rules.enabled:
            return req
        return Request(req.method, req.path, self.params(req.query), self.headers(req.headers),
                       self.body(req.body, req.content_type))  # fmt: skip

    def response(self, resp: Response) -> Response:
        if not self.rules.enabled:
            return resp
        return Response(resp.status, self.headers(resp.headers), self.body(resp.body, resp.content_type), resp.reason)

    def exchange(self, ex: Exchange) -> Exchange:
        return ex.with_(request=self.request(ex.request), response=self.response(ex.response))
