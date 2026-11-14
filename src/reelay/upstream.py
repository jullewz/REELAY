"""Forward requests to the real upstream server."""

from __future__ import annotations

import http.client
import logging
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from reelay import __version__
from reelay.bodies import Body, decode_content_encoding
from reelay.errors import ConfigError, UpstreamError
from reelay.models import Headers, Request, Response, header_get, header_without

log = logging.getLogger(__name__)

# RFC 9110 section 7.6.1: hop-by-hop headers are never forwarded.
HOP_BY_HOP = ("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
              "te", "trailer", "transfer-encoding", "upgrade")  # fmt: skip


@dataclass(frozen=True, slots=True)
class Target:
    scheme: str
    host: str
    port: int
    base_path: str

    @classmethod
    def parse(cls, url: str) -> Target:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(f"invalid upstream URL {url!r}", hint="use e.g. https://api.example.com")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(parts.scheme, parts.hostname, port, parts.path.rstrip("/"))

    @property
    def url(self) -> str:
        default = 443 if self.scheme == "https" else 80
        port = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{self.host}{port}{self.base_path}"

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def _connection_headers(headers: Headers) -> set[str]:
    """Headers listed in ``Connection`` are hop-by-hop too."""
    names: set[str] = set()
    for value in (v for k, v in headers if k.lower() == "connection"):
        names.update(token.strip().lower() for token in value.split(",") if token.strip())
    return names


def outbound_headers(req: Request, target: Target, client_ip: str | None) -> Headers:
    drop = set(HOP_BY_HOP) | _connection_headers(req.headers) | {"host", "content-length", "accept-encoding"}
    headers = [(k, v) for k, v in req.headers if k.lower() not in drop]
    headers.append(("Host", target.host_header))
    headers.append(("Accept-Encoding", "gzip, deflate"))
    if client_ip:
        prior = header_get(req.headers, "x-forwarded-for")
        headers = header_without(headers, "x-forwarded-for")
        headers.append(("X-Forwarded-For", f"{prior}, {client_ip}" if prior else client_ip))
    if not header_get(headers, "user-agent"):
        headers.append(("User-Agent", f"reelay/{__version__}"))
    return headers


class UpstreamClient:
    def __init__(self, target: Target, *, timeout: float = 30.0, verify_tls: bool = True,
                 max_body: int = 50 * 1024 * 1024) -> None:  # fmt: skip
        self.target = target
        self.timeout = timeout
        self.max_body = max_body
        self._ssl = ssl.create_default_context()
        if not verify_tls:
            log.warning("TLS certificate verification is DISABLED for %s", target.url)
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def _connect(self) -> http.client.HTTPConnection:
        if self.target.scheme == "https":
            return http.client.HTTPSConnection(self.target.host, self.target.port, timeout=self.timeout,
                                               context=self._ssl)  # fmt: skip
        return http.client.HTTPConnection(self.target.host, self.target.port, timeout=self.timeout)

    def send(self, req: Request, client_ip: str | None = None) -> tuple[Response, float]:
        """Forward ``req`` and return the decoded response and elapsed milliseconds."""
        body = req.body.to_bytes(req.content_type)
        headers = outbound_headers(req, self.target, client_ip)
        if body or req.method in ("POST", "PUT", "PATCH"):
            headers.append(("Content-Length", str(len(body))))
        path = self.target.base_path + req.target
        conn = self._connect()
        started = time.perf_counter()
        try:
            conn.putrequest(req.method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders(body or None)
            raw = conn.getresponse()
            data = raw.read(self.max_body + 1)
            elapsed = (time.perf_counter() - started) * 1000
            if len(data) > self.max_body:
                raise UpstreamError(f"upstream response for {req.method} {req.target} exceeds max_body_size")
            resp_headers: Headers = list(raw.getheaders())
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamError(f"upstream {self.target.url} failed: {type(exc).__name__}: {exc}") from None
        finally:
            conn.close()

        encoding = header_get(resp_headers, "content-encoding")
        decoded, ok = decode_content_encoding(data, encoding)
        drop = set(HOP_BY_HOP) | _connection_headers(resp_headers) | {"content-length"}
        if ok and encoding:
            drop.add("content-encoding")
        clean = [(k, v) for k, v in resp_headers if k.lower() not in drop]
        content_type = header_get(clean, "content-type")
        body_obj = Body.from_bytes(decoded, content_type) if ok else Body.from_bytes(data, "application/octet-stream")
        return Response(raw.status, clean, body_obj, raw.reason or ""), elapsed
