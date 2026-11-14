# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Do **not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/jullewz/reelay/security/advisories/new) and include the
version, platform, impact and reproduction steps. You will get an acknowledgement within 7 days.

## In scope

- secrets written to tapes, shown in the dashboard or logged despite matching a redaction rule
- ways for a web page to drive the control API cross-origin
- code execution through tapes, templates, JSONPath expressions or configuration
- crashes or hangs triggered by crafted requests, tapes or HAR files

## Out of scope

- exposing Reelay on a public interface on purpose (it warns, and documents the risk)
- secrets that match no redaction rule (add a rule, see [docs/security.md](docs/security.md))
