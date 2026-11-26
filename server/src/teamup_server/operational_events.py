"""A bounded deployment sink for redacted operational events."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from threading import Lock
from time import monotonic

_RETENTION = timedelta(hours=1)
_RETENTION_SECONDS = _RETENTION.total_seconds()
_CAPACITY = 2000
_MAX_LIMIT = 500


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Event:
    at: datetime
    tick: float
    component: str
    severity: str
    event_code: str


@dataclass(frozen=True, slots=True)
class _GatewayError(_Event):
    route: str
    method: str
    status_class: str
    error_code: str
    duration_ms: float


def _project(event: _Event) -> dict:
    entry: dict = {
        "at": event.at.isoformat().replace("+00:00", "Z"),
        "severity": event.severity,
        "component": event.component,
        "event_code": event.event_code,
    }
    if isinstance(event, _GatewayError):
        entry.update(
            route=event.route,
            method=event.method,
            status_class=event.status_class,
            error_code=event.error_code,
            duration_ms=event.duration_ms,
        )
    return entry


class OperationalEvents:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        tick: Callable[[], float] = monotonic,
    ):
        self._clock = clock
        self._tick = tick
        self._started_at = clock().astimezone(UTC)
        self._discarded_through: datetime | None = None
        self._events: deque[_Event] = deque()
        self._lock = Lock()

    def server_started(self) -> None:
        with self._lock:
            at = self._clock().astimezone(UTC)
            self._append(_Event(at, self._tick(), "host", "info", "server_started"))

    def gateway_error(
        self,
        *,
        route: str,
        method: str,
        status_class: str,
        error_code: str,
        duration_ms: float,
    ) -> None:
        """Record the normalized route and labels supplied by Gateway."""
        with self._lock:
            self._append(
                _GatewayError(
                    at=self._clock().astimezone(UTC),
                    tick=self._tick(),
                    component="gateway",
                    severity="error" if status_class == "5xx" else "warning",
                    event_code="gateway_error",
                    route=route,
                    method=method,
                    status_class=status_class,
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
            )

    def query(
        self, *, since: datetime, until: datetime, component: str | None, limit: int
    ) -> dict:
        if since.utcoffset() is None or until.utcoffset() is None:
            raise ValueError("An operational event window requires UTC offsets")
        since, until = since.astimezone(UTC), until.astimezone(UTC)
        if not (
            timedelta(0) < until - since <= _RETENTION
            and 1 <= limit <= _MAX_LIMIT
            and component in (None, "host", "gateway", "collaboration")
        ):
            raise ValueError("Invalid operational event query bounds")

        with self._lock:
            cutoff = self._clock().astimezone(UTC) - _RETENTION
            self._expire(self._tick())
            visible_since = max(since, cutoff)
            matching = [
                event
                for event in reversed(self._events)
                if visible_since <= event.at < until
                and (component is None or event.component == component)
            ]
            truncated = (
                since < max(self._started_at, cutoff)
                or (
                    self._discarded_through is not None
                    and since <= self._discarded_through
                )
                or len(matching) > limit
            )
        matching.sort(key=attrgetter("at"), reverse=True)
        return {
            "state": "available",
            "entries": [_project(event) for event in matching[:limit]],
            "truncated": truncated,
        }

    def _append(self, event: _Event) -> None:
        self._expire(event.tick)
        if len(self._events) == _CAPACITY:
            self._discard_oldest()
        self._events.append(event)

    def _expire(self, tick: float) -> None:
        # Keep the retention interval independent of changes to wall time.
        while self._events and tick - self._events[0].tick > _RETENTION_SECONDS:
            self._discard_oldest()

    def _discard_oldest(self) -> None:
        at = self._events.popleft().at
        if self._discarded_through is None or at > self._discarded_through:
            self._discarded_through = at
