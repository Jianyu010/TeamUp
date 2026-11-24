from datetime import timedelta
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from teamup_server.maintenance import (
    MAX_RETENTION_HOURS,
    RETENTION_CADENCE,
    Maintenance,
    room_retention,
)


def wait_until(condition, timeout=3.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return True
        sleep(0.005)
    return condition()


class FakeCollaboration:
    """A test double for the retention entry; it owns no state."""

    def __init__(self, results=()):
        self.results = list(results)
        self.max_ages = []

    def expire_due(self, max_age, now=None):
        self.max_ages.append(max_age)
        more_due = self.results.pop(0) if self.results else False
        return SimpleNamespace(more_due=more_due)


def test_documented_retention_constants():
    assert RETENTION_CADENCE == timedelta(seconds=15)
    assert MAX_RETENTION_HOURS == 876_000


def test_maintenance_runs_at_startup_and_repeats_to_completion():
    calls = []

    def pass_due():
        calls.append(monotonic())
        return len(calls) < 3

    with Maintenance(pass_due, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: len(calls) >= 3)
        assert calls[2] - calls[0] < 5
        sleep(0.1)
        assert len(calls) == 3


def test_maintenance_repeats_idle_checks_on_cadence_and_stops_promptly():
    calls = []

    def pass_due():
        calls.append(monotonic())
        return False

    with Maintenance(pass_due, cadence=timedelta(seconds=0.05)):
        assert wait_until(lambda: len(calls) >= 3)
    stopped = len(calls)
    sleep(0.3)
    assert len(calls) == stopped
    assert calls[1] - calls[0] >= 0.04


def test_maintenance_survives_a_failing_pass():
    calls = []

    def pass_due():
        calls.append(monotonic())
        if len(calls) == 1:
            raise RuntimeError("database busy")
        return False

    with Maintenance(pass_due, cadence=timedelta(seconds=0.05)):
        assert wait_until(lambda: len(calls) >= 3)


def test_maintenance_rejects_a_non_positive_cadence():
    with pytest.raises(ValueError):
        Maintenance(lambda: False, timedelta(seconds=0))


def test_room_retention_builder_guards_hours_and_binds_the_pass():
    assert room_retention(FakeCollaboration(), None) is None
    for invalid in (0, -1, MAX_RETENTION_HOURS + 1):
        with pytest.raises(ValueError):
            room_retention(FakeCollaboration(), invalid)
    fake = FakeCollaboration()
    with room_retention(fake, 3, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: fake.max_ages)
    assert fake.max_ages == [timedelta(hours=3)]
    busy = FakeCollaboration(results=[True, True, False])
    with room_retention(busy, 1, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: len(busy.max_ages) >= 3)
    assert busy.max_ages == [timedelta(hours=1)] * 3
