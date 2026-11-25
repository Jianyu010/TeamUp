"""Operating-system observations for the server and its database volume."""

import os
from pathlib import Path
from shutil import disk_usage
from sys import platform
from time import process_time

from .ops_metrics import ObservationSource, SourceSnapshot

HOST_METRIC_NAMES = (
    "process_uptime_seconds",
    "process_cpu_seconds_total",
    "host_cpu_busy_fraction",
    "process_resident_memory_bytes",
    "host_memory_used_bytes",
    "host_memory_total_bytes",
    "process_open_file_descriptors",
    "db_volume_free_bytes",
    "db_volume_total_bytes",
    "load_1m",
    "load_5m",
    "load_15m",
)

_PROC = Path("/proc")


class HostObservations:
    def __init__(self, db_volume: Path):
        self._db_volume = db_volume
        self._source = ObservationSource("host", HOST_METRIC_NAMES)
        self._last_host_cpu: tuple[int, int] | None = None

    def read(self) -> SourceSnapshot:
        return self._source.read(self._collect)

    def _collect(self) -> dict:
        values = {
            "process_cpu_seconds_total": process_time(),
            **_volume_observations(self._db_volume),
            **_load_averages(),
        }
        if platform == "linux":
            values.update(_linux_process())
            values.update(_linux_memory())
            values["process_open_file_descriptors"] = _linux_open_descriptors()
            values["host_cpu_busy_fraction"] = self._linux_cpu_busy()
        return values

    def _linux_cpu_busy(self) -> float | None:
        previous = self._last_host_cpu
        current = self._last_host_cpu = _linux_cpu_ticks()
        if previous is None or current is None:
            return None
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        return (total - idle) / total if total > 0 and 0 <= idle <= total else None


def _volume_observations(path: Path) -> dict:
    try:
        usage = disk_usage(path)
    except OSError:
        return {}
    return {"db_volume_free_bytes": usage.free, "db_volume_total_bytes": usage.total}


def _load_averages() -> dict:
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        return {}
    return dict(zip(("load_1m", "load_5m", "load_15m"), load, strict=True))


def _linux_process() -> dict:
    try:
        # The process name is parenthesized and can contain spaces and parentheses.
        fields = (_PROC / "self/stat").read_bytes().rpartition(b")")[2].split()
        # Fields 22 and 24 are start time (clock ticks) and resident pages.
        start_ticks, pages = int(fields[19]), int(fields[21])
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (IndexError, OSError, ValueError):
        return {}
    values: dict[str, int | float | None] = {
        "process_resident_memory_bytes": pages * page_size if pages >= 0 else None,
    }
    try:
        boot_elapsed = float((_PROC / "uptime").read_bytes().split(maxsplit=1)[0])
        elapsed = boot_elapsed - start_ticks / os.sysconf("SC_CLK_TCK")
    except (IndexError, OSError, ValueError, ZeroDivisionError):
        return values
    values["process_uptime_seconds"] = elapsed if elapsed >= 0 else None
    return values


def _linux_memory() -> dict:
    readings: dict[bytes, int] = {}
    try:
        with (_PROC / "meminfo").open("rb") as meminfo:
            for line in meminfo:
                name, _, reading = line.partition(b":")
                if name not in (b"MemTotal", b"MemAvailable"):
                    continue
                amount, unit = reading.split()
                if unit == b"kB":
                    readings[name] = int(amount) * 1024
    except (OSError, ValueError):
        return {}
    total = readings.get(b"MemTotal")
    available = readings.get(b"MemAvailable")
    used = (
        total - available
        if total is not None and available is not None and 0 <= available <= total
        else None
    )
    return {"host_memory_used_bytes": used, "host_memory_total_bytes": total}


def _linux_open_descriptors() -> int | None:
    try:
        with os.scandir(_PROC / "self/fd") as entries:
            count = sum(1 for _ in entries)
    except OSError:
        return None
    # Reading the directory temporarily opens one descriptor in this process.
    return count - 1 if count else None


def _linux_cpu_ticks() -> tuple[int, int] | None:
    try:
        with (_PROC / "stat").open("rb") as stat:
            label, *fields = stat.readline().split()
        if label != b"cpu" or len(fields) < 8:
            return None
        # Guest time is already included in user and nice. Idle includes I/O wait.
        total = sum(map(int, fields[:8]))
        idle = int(fields[3]) + int(fields[4])
    except (OSError, ValueError):
        return None
    return total, idle
