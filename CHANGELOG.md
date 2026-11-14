# Changelog

All notable changes are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-14

First public release.

### Added
- Recording reverse proxy, replay mock server, hybrid and passthrough modes, switchable at runtime.
- Matching on method, path, query, body and headers with ignore rules, four replay strategies, fuzzy fallback and
  miss diagnostics listing the closest recordings.
- Secret redaction for headers, query parameters, JSON keys and paths, form bodies and common token formats.
- Chaos engine: latency distributions, error injection, dropped connections, bandwidth throttling, seeded.
- Response templates resolved from a safe whitelist.
- Live dashboard with server-sent events, inspector, filters, stats and mode switching.
- `diff` and `verify` with structural JSON diffs, text, JSON and Markdown reports.
- OpenAPI 3.1 inference with path templating and JSON Schema 2020-12, YAML or JSON output.
- HAR 1.2 import and export, tape `filter`, `merge` and `redact`.
- `reelay.testing.replay` helper and a pytest plugin with a `reelay` fixture.

[Unreleased]: https://github.com/jullewz/reelay/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jullewz/reelay/releases/tag/v0.1.0
