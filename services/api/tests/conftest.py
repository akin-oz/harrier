"""Shared test setup.

The API token (spec 035) is fixed here rather than generated per test, so a
test that means to present it can, and a test that means to omit it still
exercises the refusal. Setting it in the environment also keeps the tests
from writing a token file into a real data directory.
"""

from __future__ import annotations

import pytest

TEST_TOKEN = "test-token-not-a-secret"


@pytest.fixture(autouse=True)
def _api_token(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setenv("HARRIER_API_TOKEN", TEST_TOKEN)


def auth() -> dict[str, str]:
    """Headers a request from the operator's own browser would carry."""
    return {"X-Harrier-Token": TEST_TOKEN}
