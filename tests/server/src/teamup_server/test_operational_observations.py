from datetime import UTC, datetime, timedelta
from functools import partial
from shutil import disk_usage

import pytest
from teamup_server import host_observations
from teamup_server.operational_events import OperationalEvents
from teamup_server.ops_metrics import ObservationSource, utc_timestamp


def test_observation_attempts_are_spaced_even_when_no_sample_is_available():
    now = 0.0
    attempts = []
    source = ObservationSource("example", ["reading"], clock=lambda: now)

    def collect():
        attempts.append(now)

    assert source.read(collect) == {
        "source": "example",
        "sampled_at": None,
        "sample_interval_seconds": 15,
        "stale_after_seconds": 45,
        "unavailable_after_seconds": 90,
        "state": "unavailable",
        "values": {"reading": None},
        "rates": None,
    }
    now = 14.999
    source.read(collect)
    now = 15.0
    source.read(collect)
    assert attempts == [0.0, 15.0]


def test_observation_retains_last_sample_until_its_monotonic_age_expires():
    now = 0.0
    at = "2026-10-01T00:00:00Z"
    source = ObservationSource(
        "example", ["reading", "missing"], clock=lambda: now, timestamp=lambda: at
    )
    fresh = source.read(lambda: {"reading": 4, "unregistered": "hidden"})
    assert fresh["values"] == {"reading": 4, "missing": None}
    assert fresh["sampled_at"] == at

    at = "2026-09-01T00:00:00Z"
    for now, state in (
        (45.0, "fresh"),
        (45.001, "stale"),
        (90.0, "stale"),
        (90.001, "unavailable"),
    ):
        snapshot = source.read(lambda: None)
        assert snapshot["state"] == state
        assert snapshot["sampled_at"] == fresh["sampled_at"]
        assert snapshot["values"]["reading"] == (None if state == "unavailable" else 4)
        assert snapshot["rates"] is None

    now = 105.0
    recovered = source.read(lambda: {"reading": 2})
    assert recovered["state"] == "fresh"
    assert recovered["sampled_at"] == at
    assert recovered["values"] == {"reading": 2, "missing": None}


def test_observation_values_are_independent_of_previous_callers():
    source = ObservationSource("example", ["rows"], clock=lambda: 0)
    rows = [{"count": 1}]
    snapshot = source.read(lambda: {"rows": rows})
    rows[0]["count"] = 2
    snapshot["values"]["rows"][0]["count"] = 3
    assert source.read(lambda: None)["values"] == {"rows": [{"count": 1}]}


def test_timestamp_is_rfc3339_utc():
    timestamp = utc_timestamp()
    assert timestamp.endswith("Z")
    assert abs(datetime.now(UTC) - datetime.fromisoformat(timestamp)) < timedelta(
        seconds=1
    )


def test_linux_host_process_and_host_gauges_use_their_own_counters(
    tmp_path, monkeypatch
):
    proc = tmp_path / "proc"
    (proc / "self/fd").mkdir(parents=True)
    for fd in range(4):
        (proc / "self/fd" / str(fd)).touch()
    process_fields = [b"S"] + [b"0"] * 21
    process_fields[19] = b"4750"
    process_fields[21] = b"20"
    (proc / "self/stat").write_bytes(b"42 (worker ) name) " + b" ".join(process_fields))
    (proc / "uptime").write_text("50.0 230.0\n")
    (proc / "stat").write_text("cpu 20 0 30 50 0 0 0 0 5 0\n")
    (proc / "meminfo").write_text("MemTotal: 2048 kB\nMemAvailable: 512 kB\n")
    monkeypatch.setattr(host_observations, "_PROC", proc)
    monkeypatch.setattr(host_observations, "platform", "linux")
    monkeypatch.setattr(
        host_observations.os,
        "sysconf",
        lambda name: {"SC_CLK_TCK": 100, "SC_PAGE_SIZE": 4096}[name],
        raising=False,
    )
    monkeypatch.setattr(
        host_observations.os, "getloadavg", lambda: (1.0, 2.0, 3.0), raising=False
    )
    tick = 0.0
    monkeypatch.setattr(
        host_observations,
        "ObservationSource",
        partial(ObservationSource, clock=lambda: tick),
    )

    host = host_observations.HostObservations(tmp_path)
    first = host.read()
    assert first["source"] == "host"
    assert first["state"] == "fresh"
    assert first["values"] == {
        "process_uptime_seconds": 2.5,
        "process_cpu_seconds_total": first["values"]["process_cpu_seconds_total"],
        "host_cpu_busy_fraction": None,
        "process_resident_memory_bytes": 20 * 4096,
        "host_memory_used_bytes": 1536 * 1024,
        "host_memory_total_bytes": 2048 * 1024,
        "process_open_file_descriptors": 3,
        "db_volume_free_bytes": first["values"]["db_volume_free_bytes"],
        "db_volume_total_bytes": disk_usage(tmp_path).total,
        "load_1m": 1.0,
        "load_5m": 2.0,
        "load_15m": 3.0,
    }
    assert first["values"]["process_cpu_seconds_total"] > 0
    assert 0 <= first["values"]["db_volume_free_bytes"] <= disk_usage(tmp_path).total

    tick += 15
    (proc / "stat").write_text("cpu 30 0 30 60 0 0 0 0 8 0\n")
    assert host.read()["values"]["host_cpu_busy_fraction"] == 0.5


def test_host_returns_null_for_unavailable_platform_and_volume_readings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(host_observations, "platform", "darwin")

    def unavailable():
        raise OSError("load average unavailable")

    monkeypatch.setattr(host_observations.os, "getloadavg", unavailable, raising=False)
    values = host_observations.HostObservations(tmp_path / "missing").read()["values"]
    assert set(values) == set(host_observations.HOST_METRIC_NAMES)
    assert values.pop("process_cpu_seconds_total") > 0
    assert all(value is None for value in values.values())


def record_gateway_error(events, *, duration_ms=1.0):
    events.gateway_error(
        route="/v1/rooms/{room_id}/search",
        method="GET",
        status_class="4xx",
        error_code="unauthorized",
        duration_ms=duration_ms,
    )


def test_operational_events_project_only_the_closed_event_fields():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    now = start
    events = OperationalEvents(clock=lambda: now)
    events.server_started()
    now += timedelta(seconds=1)
    record_gateway_error(events)
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        events.gateway_error(
            route="unmatched",
            method="GET",
            status_class="4xx",
            error_code="unauthorized",
            duration_ms=1.0,
            raw_path="marker",
        )

    result = events.query(
        since=start, until=now + timedelta(seconds=1), component=None, limit=2
    )
    assert result == {
        "state": "available",
        "entries": [
            {
                "at": "2026-10-01T00:00:01Z",
                "severity": "warning",
                "component": "gateway",
                "event_code": "gateway_error",
                "route": "/v1/rooms/{room_id}/search",
                "method": "GET",
                "status_class": "4xx",
                "error_code": "unauthorized",
                "duration_ms": 1.0,
            },
            {
                "at": "2026-10-01T00:00:00Z",
                "severity": "info",
                "component": "host",
                "event_code": "server_started",
            },
        ],
        "truncated": False,
    }
    assert (
        events.query(since=start, until=now, component="gateway", limit=1)["entries"]
        == []
    )
    assert events.query(since=start, until=now, component="host", limit=1)[
        "entries"
    ] == [result["entries"][1]]
    assert events.query(
        since=start, until=now + timedelta(seconds=1), component=None, limit=1
    ) == {"state": "available", "entries": result["entries"][:1], "truncated": True}


def test_operational_event_truncation_covers_process_start_and_one_hour_retention():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    now = start
    events = OperationalEvents(
        clock=lambda: now, tick=lambda: (now - start).total_seconds()
    )
    events.server_started()
    assert events.query(
        since=start - timedelta(seconds=1), until=start, component=None, limit=1
    ) == {
        "state": "available",
        "entries": [],
        "truncated": True,
    }

    now += timedelta(hours=1)
    result = events.query(since=start, until=now, component=None, limit=1)
    assert len(result["entries"]) == 1
    assert result["truncated"] is False

    now += timedelta(microseconds=1)
    assert events.query(
        since=start, until=start + timedelta(seconds=1), component=None, limit=1
    ) == {"state": "available", "entries": [], "truncated": True}


def test_operational_event_capacity_truncates_at_the_inclusive_since_boundary():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    now = start
    events = OperationalEvents(clock=lambda: now)
    for index in range(2001):
        record_gateway_error(events, duration_ms=float(index))
        now += timedelta(milliseconds=1)

    next_time = start + timedelta(milliseconds=1)
    overflow = events.query(since=start, until=next_time, component="gateway", limit=1)
    assert overflow == {"state": "available", "entries": [], "truncated": True}
    complete = events.query(
        since=next_time,
        until=next_time + timedelta(milliseconds=1),
        component="gateway",
        limit=1,
    )
    assert complete["truncated"] is False
    assert complete["entries"][0]["duration_ms"] == 1.0
