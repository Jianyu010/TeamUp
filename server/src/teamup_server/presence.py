"""Process-local member heartbeat observations scoped by Collaboration."""

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import monotonic_ns

from .collaboration import Collaboration

_NS_PER_MS = 1_000_000
_LEASE_NS = 45_000 * _NS_PER_MS


@dataclass(frozen=True)
class _Heartbeat:
    received_ns: int
    observed_at: str


@dataclass(frozen=True)
class _Lease:
    latest: _Heartbeat
    start: _Heartbeat


class Presence:
    def __init__(self, collaboration: Collaboration):
        self._collaboration = collaboration
        self._leases: dict[str, _Lease] = {}
        self._lock = Lock()

    def heartbeat(self, token: str) -> dict:
        with self._lock:
            receipt = _Heartbeat(monotonic_ns(), _utc_now())
        member_id = self._collaboration.get_member(token)["member_id"]
        with self._lock:
            previous = self._leases.get(member_id)
            if previous is None:
                self._leases[member_id] = _Lease(receipt, receipt)
            else:
                gap_ns = receipt.received_ns - previous.latest.received_ns
                if gap_ns > 0:
                    start = previous.start if gap_ns < _LEASE_NS else receipt
                    self._leases[member_id] = _Lease(receipt, start)
        return {"observed_at": receipt.observed_at}

    def snapshot(self, token: str) -> dict:
        member_ids = self._collaboration.authorized_presence_members(token)
        with self._lock:
            now_ns = monotonic_ns()
            as_of = _utc_now()
            members = []
            for member_id in member_ids:
                lease = self._leases.get(member_id)
                online_since, online_for_ms = None, None
                if lease is None:
                    state, observed_at, age_ms = "unknown", None, None
                else:
                    age_ns = now_ns - lease.latest.received_ns
                    state = "online" if age_ns < _LEASE_NS else "offline"
                    age_ms = (age_ns + _NS_PER_MS - 1) // _NS_PER_MS
                    observed_at = lease.latest.observed_at
                    if state == "online":
                        online_since = lease.start.observed_at
                        online_for_ms = (now_ns - lease.start.received_ns) // _NS_PER_MS
                members.append(
                    {
                        "member_id": member_id,
                        "state": state,
                        "observed_at": observed_at,
                        "age_ms": age_ms,
                        "online_since": online_since,
                        "online_for_ms": online_for_ms,
                    }
                )
        return {"as_of": as_of, "members": members}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
