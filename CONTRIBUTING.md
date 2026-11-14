# Contributing to Reelay

Thanks for helping. This guide covers the workflow and the standards a change needs to meet.

## Ground rules

- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- Report vulnerabilities privately, see [SECURITY.md](SECURITY.md).
- Open an issue before large changes so the design can be agreed first.

## Setup

```bash
git clone https://github.com/jullewz/reelay.git && cd reelay
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Checks (same as CI)

```bash
python -m pytest
ruff check . && ruff format --check .
mypy
```

## Standards

- **No runtime dependencies.** Reelay must install anywhere Python runs. Dev-only tools are fine.
- **Strict typing.** `mypy --strict` passes.
- **Never leak secrets.** Anything new that stores or displays traffic must go through `Redactor`.
- **Never block the proxy.** Dashboard and logging work must not slow request handling (see `EventBus`).
- **Tests with real sockets.** Server features get an end-to-end test in `tests/test_server.py` against the fake
  upstream in `tests/conftest.py`.
- **Cross-platform.** Use `pathlib`, avoid POSIX-only calls, keep Windows in mind for file handling.

## Changing the tape format

The tape format is a public contract ([docs/tape-format.md](docs/tape-format.md)). Backwards-incompatible changes
need a new `reelay_tape` version and a loader for the old one.

## Releasing

1. Bump `__version__` in `src/reelay/__init__.py` and update `CHANGELOG.md`.
2. Regenerate visuals if output changed: `python scripts/make_demo.py`.
3. Tag `vX.Y.Z` and push. The release workflow builds, tests and publishes artifacts with checksums.
