"""Helpers for using Reelay inside test suites.

Run a replay server for the duration of a ``with`` block::

    from reelay.testing import replay

    def test_profile_page():
        with replay("tests/tapes/github.tape") as api:
            client = GitHubClient(base_url=api.url)
            assert client.user("octocat").name == "The Octocat"

Or record once and replay forever after (great for CI)::

    with replay("tests/tapes/github.tape", target="https://api.github.com", mode="hybrid") as api:
        ...

The pytest plugin (``reelay.pytest_plugin``) exposes the same thing as a
``reelay`` fixture driven by ``@pytest.mark.reelay(tape=...)``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from reelay.chaos import ChaosEngine, ChaosRule
from reelay.config import Settings
from reelay.matching import MatchSettings
from reelay.server import ReelayServer
from reelay.tape import Tape


@contextmanager
def replay(
    tape: str | Path,
    *,
    target: str | None = None,
    mode: str = "replay",
    on_miss: str = "error",
    match: MatchSettings | None = None,
    chaos: list[ChaosRule] | None = None,
    seed: int | None = 0,
    settings: Settings | None = None,
) -> Iterator[ReelayServer]:
    """Start a Reelay server on a free local port and stop it afterwards."""
    cfg = settings or Settings()
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    cfg.server.mode = mode
    cfg.server.on_miss = on_miss
    cfg.server.dashboard = False
    if target:
        cfg.server.target = target
    if match:
        cfg.match = match
    if chaos is not None:
        cfg.chaos = ChaosEngine(chaos, seed)
    path = Path(tape)
    loaded = (
        Tape.open_or_create(path, target) if mode in ("record", "hybrid") or on_miss == "record" else Tape.load(path)
    )
    server = ReelayServer(cfg, loaded)
    with server:
        yield server
