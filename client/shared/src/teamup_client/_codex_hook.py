"""Register a local Codex thread from its lifecycle event."""

import asyncio
from contextlib import suppress

from ._config import ConfigError, read_config
from ._protocol import RESPONSE_LIMIT, decode_json, is_uuid
from .ipc import ClientError, call


async def register_thread(config_path, harness_instance_id, stdin):
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
    thread = event.get("session_id")
    if not is_uuid(thread):
        return

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
        raise ConfigError("The Codex hook needs a configured TeamUp installation")
    with suppress(ClientError, TimeoutError):
        async with asyncio.timeout(5):
            await call(
                config.socket_path,
                install.binding_key,
                "register",
                {
                    "name": f"Codex {thread[-8:].lower()}",
                    "description": "Available for TeamUp collaboration from a local Codex session.",
                },
                session={"kind": "codex", "id": thread},
            )
