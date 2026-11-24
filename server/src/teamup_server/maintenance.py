"""Scheduling for Collaboration's retention maintenance."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event, Thread
from typing import Self

from .collaboration import Collaboration

_LOGGER = logging.getLogger(__name__)
RETENTION_CADENCE = timedelta(seconds=15)
MAX_RETENTION_HOURS = 876_000
_STOP_TIMEOUT = timedelta(seconds=5)

RetentionPassDue = Callable[[], bool]


class Maintenance:
    """Runs one retention pass at startup, then repeats it on a fixed cadence.

    Passes run on a daemon thread, so streaming responses never wait on
    retention work. A pass that reports more due work is repeated at once;
    the cadence only spaces idle checks, and stopping wakes the thread
    immediately. The component owns no business state and no SQLite: the
    scheduled callable is the owner's published maintenance entry.
    """

    def __init__(self, pass_due: RetentionPassDue, cadence: timedelta):
        if cadence <= timedelta(0):
            raise ValueError("The retention cadence must be positive")
        self._pass_due = pass_due
        self._cadence = cadence.total_seconds()
        self._stopping = Event()
        self._worker: Thread | None = None

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("Retention maintenance already started")
        self._stopping.clear()
        worker = Thread(target=self._run, name="teamup-retention", daemon=True)
        self._worker = worker
        worker.start()

    def stop(self) -> None:
        self._stopping.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(_STOP_TIMEOUT.total_seconds())

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                more_due = self._pass_due()
            except Exception:
                _LOGGER.exception("A retention maintenance pass failed")
                more_due = False
            if more_due and not self._stopping.is_set():
                continue
            self._stopping.wait(self._cadence)


def retention(
    collaboration: Collaboration,
    room_hours: int | None,
    dm_hours: int | None,
    cadence: timedelta = RETENTION_CADENCE,
) -> Maintenance | None:
    """Build the shared retention scheduler, or ``None`` when both sides are off.

    Each family's flag is validated on its own; one enabled side is enough
    to schedule the single maintenance loop, and omitting both keeps the
    runtime free of any retention scheduling.
    """
    room_age = _validated_age("--room-retention-hours", room_hours)
    dm_age = _validated_age("--dm-retention-hours", dm_hours)
    if room_age is None and dm_age is None:
        return None
    return Maintenance(
        partial(_pass_more_due, collaboration, room_age, dm_age), cadence
    )


def _validated_age(flag: str, hours: int | None) -> timedelta | None:
    if hours is None:
        return None
    if not 1 <= hours <= MAX_RETENTION_HOURS:
        raise ValueError(
            f"{flag} must be an integer between 1 and {MAX_RETENTION_HOURS}"
        )
    return timedelta(hours=hours)


def _pass_more_due(
    collaboration: Collaboration,
    room_max_age: timedelta | None,
    dm_max_age: timedelta | None,
) -> bool:
    # Both enabled families purge inside the same cycle without short
    # circuit, so a busy room backlog never defers the DM pass (or the
    # other way round); either side's more-due work repeats the cycle.
    more_due = False
    if room_max_age is not None:
        more_due = collaboration.expire_due(room_max_age).more_due
    if dm_max_age is not None:
        more_due = (
            collaboration.expire_due_dms(datetime.now(UTC) - dm_max_age)[1] or more_due
        )
    return more_due
