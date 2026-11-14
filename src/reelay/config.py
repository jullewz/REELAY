"""Configuration (``reelay.toml``). Every setting also has a command-line flag.

Lookup order: ``--config PATH``, ``$REELAY_CONFIG``, ``./reelay.toml``. A
config file is optional: ``reelay record --target URL`` works without one.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reelay.chaos import ChaosEngine, ChaosRule, Latency, parse_bandwidth
from reelay.errors import ConfigError
from reelay.matching import DIMENSIONS, STRATEGIES, MatchSettings
from reelay.redact import PLACEHOLDER, RedactionRules
from reelay.util import expand_path, parse_duration, parse_size

MODES = ("record", "replay", "hybrid", "passthrough")
ON_MISS = ("error", "proxy", "record")
CONFIG_FILENAME = "reelay.toml"

_ALLOWED: dict[str, set[str]] = {
    "": {"server", "match", "redact", "chaos", "diff"},
    "server": {
        "host",
        "port",
        "mode",
        "tape",
        "target",
        "timeout",
        "verify_tls",
        "max_body_size",
        "control_prefix",
        "dashboard",
        "on_miss",
        "cors",
    },
    "match": {"on", "ignore_query", "ignore_body", "headers", "strategy", "trailing_slash", "fuzzy"},
    "redact": {
        "enabled",
        "headers",
        "query",
        "json_keys",
        "json_paths",
        "patterns",
        "keep_defaults",
        "placeholder",
    },
    "chaos": {"enabled", "seed", "rule"},
    "chaos.rule": {"route", "latency", "error_rate", "error_status", "drop_rate", "bandwidth"},
    "diff": {"ignore"},
}


@dataclass(slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    mode: str = "replay"
    tape: Path | None = None
    target: str | None = None
    timeout: float = 30.0
    verify_tls: bool = True
    max_body_size: int = 50 * 1024 * 1024
    control_prefix: str = "/__reelay"
    dashboard: bool = True
    on_miss: str = "error"
    cors: bool = False


@dataclass(slots=True)
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    match: MatchSettings = field(default_factory=MatchSettings)
    redact: RedactionRules = field(default_factory=RedactionRules)
    chaos: ChaosEngine = field(default_factory=ChaosEngine)
    diff_ignore: list[str] = field(default_factory=list)
    source: Path | None = None


def _check(table: dict[str, Any], section: str) -> None:
    allowed = _ALLOWED[section]
    where = f"[{section}]" if section else "the top level"
    for key in table:
        if key not in allowed:
            close = difflib.get_close_matches(key, allowed, n=1)
            hint = f"did you mean {close[0]!r}?" if close else f"allowed: {', '.join(sorted(allowed))}"
            raise ConfigError(f"unknown key {key!r} in {where}", hint=hint)


def _typed(value: Any, kind: type | tuple[type, ...], where: str) -> Any:
    if isinstance(value, bool) and kind in (int, float, (int, float)):
        raise ConfigError(f"{where} must be a number")
    if not isinstance(value, kind):
        name = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        raise ConfigError(f"{where} must be {name}, got {type(value).__name__}")
    return value


def _strs(value: Any, where: str) -> list[str]:
    _typed(value, list, where)
    for i, item in enumerate(value):
        _typed(item, str, f"{where}[{i}]")
    return list(value)


def _choice(value: Any, choices: tuple[str, ...], where: str) -> str:
    _typed(value, str, where)
    if value not in choices:
        raise ConfigError(f"{where} must be one of {', '.join(choices)}, got {value!r}")
    return str(value)


def parse_settings(data: dict[str, Any], source: Path | None = None) -> Settings:
    _check(data, "")
    s = Settings(source=source)

    srv = _typed(data.get("server", {}), dict, "[server]")
    _check(srv, "server")
    st = s.server
    st.host = _typed(srv.get("host", st.host), str, "server.host")
    st.port = _typed(srv.get("port", st.port), int, "server.port")
    if not 0 <= st.port <= 65535:
        raise ConfigError("server.port must be between 0 and 65535")
    st.mode = _choice(srv.get("mode", st.mode), MODES, "server.mode")
    if "tape" in srv:
        st.tape = Path(expand_path(_typed(srv["tape"], str, "server.tape")))
    st.target = _typed(srv["target"], str, "server.target") if "target" in srv else None
    try:
        st.timeout = parse_duration(srv.get("timeout", st.timeout))
        st.max_body_size = parse_size(srv.get("max_body_size", st.max_body_size))
    except ValueError as exc:
        raise ConfigError(f"[server]: {exc}") from None
    st.verify_tls = _typed(srv.get("verify_tls", True), bool, "server.verify_tls")
    st.dashboard = _typed(srv.get("dashboard", True), bool, "server.dashboard")
    st.cors = _typed(srv.get("cors", False), bool, "server.cors")
    st.on_miss = _choice(srv.get("on_miss", st.on_miss), ON_MISS, "server.on_miss")
    prefix = _typed(srv.get("control_prefix", st.control_prefix), str, "server.control_prefix")
    if not prefix.startswith("/") or prefix == "/" or prefix.endswith("/"):
        raise ConfigError("server.control_prefix must look like '/__reelay'")
    st.control_prefix = prefix

    m = _typed(data.get("match", {}), dict, "[match]")
    _check(m, "match")
    try:
        s.match = MatchSettings(
            on=tuple(_strs(m.get("on", list(MatchSettings().on)), "match.on")),
            ignore_query=frozenset(_strs(m.get("ignore_query", []), "match.ignore_query")),
            ignore_body=_strs(m.get("ignore_body", []), "match.ignore_body"),
            headers=tuple(_strs(m.get("headers", []), "match.headers")),
            strategy=_typed(m.get("strategy", "sequential"), str, "match.strategy"),
            trailing_slash=_typed(m.get("trailing_slash", False), bool, "match.trailing_slash"),
            fuzzy=_typed(m.get("fuzzy", False), bool, "match.fuzzy"),
        )
    except ValueError as exc:
        hint = f"dimensions: {', '.join(DIMENSIONS)}; strategies: {', '.join(STRATEGIES)}"
        raise ConfigError(f"[match]: {exc}", hint=hint) from None

    r = _typed(data.get("redact", {}), dict, "[redact]")
    _check(r, "redact")
    try:
        s.redact = RedactionRules.build(
            enabled=_typed(r.get("enabled", True), bool, "redact.enabled"),
            extra_headers=_strs(r.get("headers", []), "redact.headers"),
            extra_params=_strs(r.get("query", []), "redact.query"),
            extra_json_keys=_strs(r.get("json_keys", []), "redact.json_keys"),
            json_paths=_strs(r.get("json_paths", []), "redact.json_paths"),
            extra_patterns=_strs(r.get("patterns", []), "redact.patterns"),
            keep_defaults=_typed(r.get("keep_defaults", True), bool, "redact.keep_defaults"),
            placeholder=_typed(r.get("placeholder", PLACEHOLDER), str, "redact.placeholder"),
        )
    except ValueError as exc:  # re.error is a ValueError subclass in practice via compile
        raise ConfigError(f"[redact]: {exc}") from None
    except Exception as exc:
        raise ConfigError(f"[redact]: invalid pattern: {exc}") from None

    c = _typed(data.get("chaos", {}), dict, "[chaos]")
    _check(c, "chaos")
    rules: list[ChaosRule] = []
    for i, raw in enumerate(_typed(c.get("rule", []), list, "[[chaos.rule]]")):
        where = f"[[chaos.rule]] #{i + 1}"
        _typed(raw, dict, where)
        _check(raw, "chaos.rule")
        try:
            rules.append(ChaosRule(
                route=_typed(raw.get("route", "/**"), str, f"{where}.route"),
                latency=Latency.parse(raw["latency"]) if "latency" in raw else None,
                error_rate=float(_typed(raw.get("error_rate", 0.0), (int, float), f"{where}.error_rate")),
                error_status=_typed(raw.get("error_status", 503), int, f"{where}.error_status"),
                drop_rate=float(_typed(raw.get("drop_rate", 0.0), (int, float), f"{where}.drop_rate")),
                bandwidth=parse_bandwidth(_typed(raw["bandwidth"], str, f"{where}.bandwidth"))
                if "bandwidth" in raw else None,
            ))  # fmt: skip
        except ValueError as exc:
            raise ConfigError(f"{where}: {exc}") from None
    seed = c.get("seed")
    if seed is not None:
        _typed(seed, int, "chaos.seed")
    s.chaos = ChaosEngine(rules, seed, _typed(c.get("enabled", True), bool, "chaos.enabled"))

    d = _typed(data.get("diff", {}), dict, "[diff]")
    _check(d, "diff")
    s.diff_ignore = _strs(d.get("ignore", []), "diff.ignore")
    return s


def find_config(explicit: str | None) -> Path | None:
    if explicit:
        return Path(expand_path(explicit))
    env = os.environ.get("REELAY_CONFIG")
    if env:
        return Path(expand_path(env))
    local = Path.cwd() / CONFIG_FILENAME
    return local if local.is_file() else None


def load_settings(explicit: str | None = None) -> Settings:
    path = find_config(explicit)
    if path is None:
        return Settings()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return parse_settings(data, path)


STARTER = """# Reelay configuration. Every key is optional.
# Reference: https://github.com/jullewz/reelay/blob/main/docs/configuration.md

[server]
host = "127.0.0.1"         # use 0.0.0.0 only on trusted networks
port = 8080
mode = "hybrid"            # record | replay | hybrid | passthrough
tape = "tapes/api.tape"
target = "https://api.example.com"
on_miss = "error"          # replay mode: error | proxy | record
timeout = "30s"
dashboard = true           # live UI at http://127.0.0.1:8080/__reelay/

[match]
on = ["method", "path", "query", "body"]
ignore_query = ["_", "cacheBuster"]
ignore_body = ["$.requestId", "$..timestamp"]
strategy = "sequential"    # sequential | cycle | first | last

[redact]
headers = ["X-Internal-Token"]
json_keys = ["ssn"]
# patterns = ['acct_[0-9a-f]{24}']

[chaos]
enabled = false
seed = 42

[[chaos.rule]]
route = "GET /search/**"
latency = "100ms..600ms"
error_rate = 0.05
error_status = 503

[diff]
ignore = ["$..updated_at", "$..etag"]
"""
