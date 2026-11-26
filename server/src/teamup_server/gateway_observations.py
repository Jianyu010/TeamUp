"""Gateway-owned transport observations, limited to published route templates."""

from collections import Counter, defaultdict
from threading import Lock
from time import monotonic

from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .operational_events import OperationalEvents
from .ops_metrics import ObservationSource

_SSE_ROUTE = "/v1/events"
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_VALUES = (
    "http_requests_total",
    "http_request_duration_seconds_total",
    "http_request_duration_count",
    "http_error_responses_total",
    "active_http_requests",
    "active_sse_streams",
    "sse_connections_total",
)
ERROR_CODE_KEY = "teamup.error_code"


def _observe_http(scope: Scope) -> bool:
    if scope["type"] != "http":
        return False
    path = scope["path"]
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path + "/"):
        path = path[len(root_path) :]
    return path.split("/", 3)[1:3] not in (["v1", "dms"], ["v1", "inbox"])


class GatewayObservations:
    def __init__(self, events: OperationalEvents | None, error_codes: frozenset[str]):
        self._events = events
        self._error_codes = error_codes
        self._lock = Lock()
        self._source = ObservationSource("gateway", _VALUES)
        self._active_http_requests = 0
        self._active_sse_streams = 0
        self._sse_connections_total = 0
        self._traffic: Counter[tuple[str, str, str]] = Counter()
        self._durations: defaultdict[str, float] = defaultdict(float)
        self._duration_counts: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()

    def request_started(self) -> None:
        with self._lock:
            self._active_http_requests += 1

    def response_started(self, scope: Scope, status: int, duration: float) -> bool:
        matched = scope.get("route")
        route = matched.path if isinstance(matched, APIRoute) else "unmatched"
        method = scope["method"] if scope["method"] in _METHODS else "other"
        status_class = f"{status // 100}xx"
        is_stream = route == _SSE_ROUTE and status == 200
        reported_code = scope.get(ERROR_CODE_KEY)
        error_code = (
            reported_code
            if reported_code in self._error_codes
            else "internal_error"
            if status >= 500
            else "http_error"
        )
        with self._lock:
            self._active_http_requests -= 1
            self._traffic[route, method, status_class] += 1
            self._durations[route] += duration
            self._duration_counts[route] += 1
            if is_stream:
                self._active_sse_streams += 1
                self._sse_connections_total += 1
            if status >= 400:
                self._errors[error_code] += 1
        if status >= 400 and self._events is not None:
            self._events.gateway_error(
                route=route,
                method=method,
                status_class=status_class,
                error_code=error_code,
                duration_ms=duration * 1000,
            )
        return is_stream

    def request_finished(self, *, responded: bool, is_stream: bool) -> None:
        with self._lock:
            if not responded:
                self._active_http_requests -= 1
            if is_stream:
                self._active_sse_streams -= 1

    def read(self) -> dict:
        return self._source.read(self._collect)

    def _collect(self) -> dict:
        with self._lock:
            return {
                "http_requests_total": [
                    {
                        "route": route,
                        "method": method,
                        "status_class": status,
                        "count": count,
                    }
                    for (route, method, status), count in sorted(self._traffic.items())
                ],
                "http_request_duration_seconds_total": [
                    {"route": route, "seconds": seconds}
                    for route, seconds in sorted(self._durations.items())
                ],
                "http_request_duration_count": [
                    {"route": route, "count": count}
                    for route, count in sorted(self._duration_counts.items())
                ],
                "http_error_responses_total": [
                    {"code": code, "count": count}
                    for code, count in sorted(self._errors.items())
                ],
                "active_http_requests": self._active_http_requests,
                "active_sse_streams": self._active_sse_streams,
                "sse_connections_total": self._sse_connections_total,
            }


class GatewayTraffic:
    def __init__(self, app: ASGIApp, observations: GatewayObservations):
        self._app = app
        self._observations = observations

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The transport omits DM and shared inbox routes. It never uses the
        # requested path, headers, query, or body to label an observation.
        if not _observe_http(scope):
            await self._app(scope, receive, send)
            return

        started = monotonic()
        responded = is_stream = False
        self._observations.request_started()

        async def observe_send(message: Message) -> None:
            nonlocal responded, is_stream
            if message["type"] == "http.response.start":
                is_stream = self._observations.response_started(
                    scope, message["status"], monotonic() - started
                )
                responded = True
            await send(message)

        try:
            await self._app(scope, receive, observe_send)
        except Exception:
            if not responded:
                self._observations.response_started(scope, 500, monotonic() - started)
                responded = True
            raise
        finally:
            self._observations.request_finished(
                responded=responded, is_stream=is_stream
            )
