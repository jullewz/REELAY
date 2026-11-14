"""pytest plugin: a ``reelay`` fixture that serves a tape during a test.

Activated automatically when Reelay is installed (``pytest11`` entry point)::

    import pytest

    @pytest.mark.reelay(tape="tests/tapes/weather.tape")
    def test_forecast(reelay):
        assert get_forecast(base_url=reelay.url)["city"] == "Helsinki"

Marker options: ``tape`` (required), ``target``, ``mode`` (default
``replay``), ``on_miss`` (default ``error``).

Command-line option ``--reelay-record`` switches every marked test to
``hybrid`` mode, which records missing exchanges from ``target``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reelay.server import ReelayServer
from reelay.testing import replay


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("reelay")
    group.addoption("--reelay-record", action="store_true", default=False,
                    help="record missing exchanges for tests marked with @pytest.mark.reelay")  # fmt: skip


def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "reelay(tape, target=None, mode='replay', on_miss='error'): "
                                       "serve a Reelay tape to this test through the `reelay` fixture")  # fmt: skip


@pytest.fixture  # type: ignore[untyped-decorator,unused-ignore]
def reelay(request: pytest.FixtureRequest) -> Iterator[ReelayServer]:
    marker = request.node.get_closest_marker("reelay")
    if marker is None or "tape" not in marker.kwargs:
        raise pytest.UsageError("the reelay fixture needs @pytest.mark.reelay(tape='path/to.tape')")
    opts = dict(marker.kwargs)
    tape = Path(opts.pop("tape"))
    if not tape.is_absolute():
        tape = Path(str(request.config.rootpath)) / tape
    if request.config.getoption("--reelay-record"):
        opts["mode"] = "hybrid"
    with replay(tape, **opts) as server:
        yield server
