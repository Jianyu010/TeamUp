"""Process-local sampling for owner-provided maintenance observations."""

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Any

_SAMPLE_INTERVAL_SECONDS = 15
_STALE_AFTER_SECONDS = 45
_UNAVAILABLE_AFTER_SECONDS = 90

type SourceSnapshot = dict[str, Any]


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _Sample:
    tick: float
    at: str
    values: dict


class ObservationSource:
    """Cache successful observations. The owner signals collection failure with None."""

    def __init__(
        self,
        source: str,
        names: Iterable[str],
        *,
        clock: Callable[[], float] = monotonic,
        timestamp: Callable[[], str] = utc_timestamp,
    ):
        self._source = source
        self._names = tuple(names)
        self._clock = clock
        self._timestamp = timestamp
        self._last_attempt: float | None = None
        self._sample: _Sample | None = None
        self._lock = Lock()

    def read(self, collector: Callable[[], dict | None]) -> SourceSnapshot:
        with self._lock:
            now = self._clock()
            if (
                self._last_attempt is None
                or now - self._last_attempt >= _SAMPLE_INTERVAL_SECONDS
            ):
                self._last_attempt = now
                collected = collector()
                now = self._clock()
                if collected is not None:
                    values = {name: collected.get(name) for name in self._names}
                    self._sample = _Sample(now, self._timestamp(), deepcopy(values))

            sample = self._sample
            state = "unavailable"
            values = dict.fromkeys(self._names)
            if sample is not None and now - sample.tick <= _UNAVAILABLE_AFTER_SECONDS:
                state = (
                    "fresh" if now - sample.tick <= _STALE_AFTER_SECONDS else "stale"
                )
                values = deepcopy(sample.values)
            return {
                "source": self._source,
                "sampled_at": sample.at if sample is not None else None,
                "sample_interval_seconds": _SAMPLE_INTERVAL_SECONDS,
                "stale_after_seconds": _STALE_AFTER_SECONDS,
                "unavailable_after_seconds": _UNAVAILABLE_AFTER_SECONDS,
                "state": state,
                "values": values,
                "rates": None,
            }
