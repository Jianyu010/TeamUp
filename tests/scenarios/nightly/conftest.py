"""Shared installed fixtures for nightly room sync scenarios."""

import pytest

from tests.scenarios._room_sync import room_sync_stack


@pytest.fixture
def stack(ws, workspace_venv):
    with room_sync_stack(ws, workspace_venv) as active:
        yield active
