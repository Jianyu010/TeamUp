"""Scheduling for Collaboration's retention maintenance."""

import logging
from collections.abc import Callable
from datetime import timedelta
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


def room_retention(
    collaboration: Collaboration,
    hours: int | None,
    cadence: timedelta = RETENTION_CADENCE,
) -> Maintenance | None:
    """Build the room retention scheduler, or ``None`` when retention is off."""
    if hours is None:
        return None
    if not 1 <= hours <= MAX_RETENTION_HOURS:
        raise ValueError(
            f"--room-retention-hours must be an integer between 1 and {MAX_RETENTION_HOURS}"
        )
    return Maintenance(
        partial(_pass_more_due, collaboration, timedelta(hours=hours)), cadence
    )


def _pass_more_due(collaboration: Collaboration, max_age: timedelta) -> bool:
    return collaboration.expire_due(max_age).more_due
