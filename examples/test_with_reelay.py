"""Example: testing an API client against a Reelay tape.

Run with:  pytest examples/test_with_reelay.py
The tape lives in tests/tapes/demo.tape and holds clearly fake sample data.
"""

from __future__ import annotations

import json
from urllib.request import urlopen

import pytest

from reelay.testing import replay


def greet(base_url: str, name: str) -> str:
    with urlopen(f"{base_url}/greeting?name={name}") as resp:
        return str(json.load(resp)["message"])


def test_with_context_manager() -> None:
    with replay("tests/tapes/demo.tape") as api:
        assert greet(api.url, "world") == "hello, world"


@pytest.mark.reelay(tape="tests/tapes/demo.tape")
def test_with_fixture(reelay) -> None:  # type: ignore[no-untyped-def]
    assert greet(reelay.url, "world") == "hello, world"
