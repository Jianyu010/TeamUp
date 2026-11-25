"""Local desktop notices for received mentions. They never start a model turn."""

import asyncio
import re
import shutil
import sys
import unicodedata
from contextlib import suppress

# notify-send has no subtitle, so the room name leads the body.
_LINUX = (
    "sh",
    "-c",
    'exec notify-send --app-name=TeamUp -- "$1" "$2: $3"',
    "teamup-notify",
)
TITLE_LIMIT = 80
PREVIEW_LIMIT = 140
TIMEOUT = 5


def default_command(platform=sys.platform):
    if platform.startswith("linux") and shutil.which("notify-send"):
        return _LINUX
    return ()


def clean(text, limit):
    """Drop control and format characters, fold whitespace and truncate."""
    printable = "".join(
        " " if char.isspace() else char
        for char in text
        if char.isspace() or unicodedata.category(char)[0] != "C"
    )
    folded = re.sub(r" +", " ", printable).strip()
    if len(folded) <= limit:
        return folded
    return folded[: limit - 1].rstrip() + "…"


class DesktopNotifier:
    def __init__(self, command):
        self._command = tuple(command)

    async def show(self, title, subtitle, body):
        """Return True only when the notifier command exits successfully."""
        if not self._command:
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                title,
                subtitle,
                body,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            async with asyncio.timeout(TIMEOUT):
                return await process.wait() == 0
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return False
