from datetime import timedelta
from time import monotonic, sleep

import pytest
from teamup_server.maintenance import MAINTENANCE_CADENCE, Maintenance


def wait_until(condition, timeout=3.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return True
        sleep(0.005)
    return condition()


def test_documented_maintenance_cadence():
    assert MAINTENANCE_CADENCE == timedelta(seconds=15)


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
