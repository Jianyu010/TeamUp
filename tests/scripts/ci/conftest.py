"""Shared fixtures for the CI script tests."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.ci import gate, merge

_REAL = gate.load_policy()


def _policy(**override: Any) -> Any:
    return lambda *_args, **_kwargs: {**_REAL, **override}


@pytest.fixture(autouse=True)
def owner_review_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests pin `require_ready` on, whatever policy.json says today."""
    monkeypatch.setattr(gate, "load_policy", _policy(require_ready=True))
    monkeypatch.setattr(merge, "load_policy", _policy(require_ready=True))


@pytest.fixture
def owner_review_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`require_ready` off, as policy.json has it now."""
    monkeypatch.setattr(gate, "load_policy", _policy(require_ready=False))
    monkeypatch.setattr(merge, "load_policy", _policy(require_ready=False))
