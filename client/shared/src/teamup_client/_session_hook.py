"""Register a local harness session from its lifecycle event."""

import asyncio
import hashlib
from contextlib import suppress
from pathlib import Path

from ._config import ConfigError, read_config
from ._protocol import RESPONSE_LIMIT, decode_json, is_string, is_uuid
from .ipc import ClientError, call

HARNESS_NAMES = {"codex": "Codex", "claude-code": "Claude"}


def _workspace_contains(workspace: Path | None, cwd: object) -> bool:
    if workspace is None or not workspace.is_absolute() or not isinstance(cwd, str):
        return False
    directory = Path(cwd)
    if not directory.is_absolute():
        return False
    try:
        root = workspace.resolve(strict=True)
        directory = directory.resolve(strict=True)
        return root.is_dir() and directory.is_dir() and any(
            root.samefile(parent) for parent in (directory, *directory.parents)
        )
    except (OSError, RuntimeError, ValueError):
        return False


async def register_session(config_path, harness_instance_id, stdin, *, kind, workspace):
    try:
        data = stdin.read(RESPONSE_LIMIT + 1)
        if len(data) > RESPONSE_LIMIT:
            return
        event = decode_json(data)
    except (OSError, ValueError, RecursionError):
        return
    if not isinstance(event, dict) or event.get("hook_event_name") not in (
        "SessionStart",
        "UserPromptSubmit",
    ):
        return
    if not _workspace_contains(workspace, event.get("cwd")):
        return
    session_id = event.get("session_id")
    if is_uuid(session_id):
        suffix = session_id[-8:].lower()
    elif kind == "claude-code" and is_string(session_id, limit=128):
        suffix = hashlib.sha256(session_id.encode()).hexdigest()[:8]
    else:
        return

    name = HARNESS_NAMES[kind]
    config = read_config(config_path)
    install = next(
        (
            item
            for item in config.installs
            if item.harness_instance_id == harness_instance_id
        ),
        None,
    )
    if install is None:
        raise ConfigError(f"The {name} hook needs a configured TeamUp installation")
    with suppress(ClientError, TimeoutError):
        async with asyncio.timeout(5):
            await call(
                config.socket_path,
                install.binding_key,
                "register",
                {
                    "name": f"{name} {suffix}",
                    "description": f"Available for TeamUp collaboration from a local {name} session.",
                },
                session={"kind": kind, "id": session_id},
            )
