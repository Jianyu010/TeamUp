"""Best-effort notice output isolated from the Client event loop."""

import os
from contextlib import suppress
from queue import Full, Queue
from threading import Thread

from ._protocol import frame


class NoticeOutput:
    def __init__(self):
        self._pending: Queue[bytes] = Queue(maxsize=128)
        Thread(target=self._write, name="teamup-notices", daemon=True).start()

    def emit(self, notice):
        with suppress(Full):
            self._pending.put_nowait(frame(notice))

    def _write(self):
        while True:
            with suppress(OSError):
                # Write to the fd directly so a blocked pipe holds no Python stream lock.
                os.write(2, self._pending.get())
