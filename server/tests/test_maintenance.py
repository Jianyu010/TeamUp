from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from teamup_server.maintenance import (
    MAX_RETENTION_HOURS,
    RETENTION_CADENCE,
    Maintenance,
    retention,
)


def wait_until(condition, timeout=3.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return True
        sleep(0.005)
    return condition()


class FakeCollaboration:
    """A test double for the retention entries; it owns no state."""

    def __init__(self, room_results=(), dm_results=()):
        self.room_results = list(room_results)
        self.dm_results = list(dm_results)
        self.max_ages = []
        self.dm_cutoffs = []

    def expire_due(self, max_age, now=None):
        self.max_ages.append(max_age)
        more_due = self.room_results.pop(0) if self.room_results else False
        return SimpleNamespace(more_due=more_due)

    def expire_due_dms(self, cutoff):
        self.dm_cutoffs.append(cutoff)
        more_due = self.dm_results.pop(0) if self.dm_results else False
        return 0, more_due


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


def test_retention_builder_guards_each_age_and_binds_the_room_pass():
    assert retention(FakeCollaboration(), None, None) is None
    for invalid in (0, -1, MAX_RETENTION_HOURS + 1):
        with pytest.raises(ValueError):
            retention(FakeCollaboration(), invalid, None)
        with pytest.raises(ValueError):
            retention(FakeCollaboration(), None, invalid)
    fake = FakeCollaboration()
    with retention(fake, 3, None, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: fake.max_ages)
    assert fake.max_ages == [timedelta(hours=3)]
    assert fake.dm_cutoffs == []
    busy = FakeCollaboration(room_results=[True, True, False])
    with retention(busy, 1, None, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: len(busy.max_ages) >= 3)
    assert busy.max_ages == [timedelta(hours=1)] * 3
    assert busy.dm_cutoffs == []


def test_retention_builder_binds_the_dm_pass_on_its_own():
    fake = FakeCollaboration()
    with retention(fake, None, 2, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: fake.dm_cutoffs)
    assert fake.max_ages == []
    assert len(fake.dm_cutoffs) == 1
    (cutoff,) = fake.dm_cutoffs
    age = datetime.now(UTC) - cutoff
    assert timedelta(hours=2) <= age < timedelta(hours=2, minutes=1)
    busy = FakeCollaboration(dm_results=[True, True, False])
    with retention(busy, None, 1, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: len(busy.dm_cutoffs) >= 3)
    assert len(busy.dm_cutoffs) == 3
    assert busy.max_ages == []


def test_retention_builder_evaluates_both_enabled_passes_without_short_circuit():
    both = FakeCollaboration(room_results=[True, True, False])
    with retention(both, 1, 2, cadence=timedelta(seconds=30)):
        assert wait_until(lambda: len(both.max_ages) >= 3)
    # Every cycle also evaluated the DM pass while the rooms stayed due.
    assert both.max_ages == [timedelta(hours=1)] * 3
    assert len(both.dm_cutoffs) == 3
    now = datetime.now(UTC)
    assert all(
        timedelta(hours=2) <= now - cutoff < timedelta(hours=2, minutes=1)
        for cutoff in both.dm_cutoffs
    )
