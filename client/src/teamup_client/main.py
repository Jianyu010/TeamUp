"""Composition and command-line entry points for TeamUp Client."""

import argparse
import asyncio
import json
import os
import secrets
import signal
import sqlite3
import sys
from contextlib import suppress
from functools import partial
from pathlib import Path

from ._config import ConfigError, locked_config
from ._coordinator import Coordinator
from ._daemon import handle
from ._desktop import DesktopNotifier, default_command
from ._local import LocalError, listen
from ._login import login
from ._notice_output import NoticeOutput
from ._notices import open_store
from ._upstream import connect
from .ipc import ClientError, call


async def _serve(path):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        with (
            locked_config(path, remove_stale_socket=True) as (config, _, _),
            open_store(config.socket_path) as notices,
        ):
            async with connect(config) as upstream:
                command = config.notify_command
                notifier = DesktopNotifier(
                    default_command() if command is None else command
                )
                coordinator = Coordinator(
                    config, upstream, notices, NoticeOutput(), notifier
                )
                on_call = partial(handle, coordinator=coordinator)
                async with (
                    listen(config.socket_path, on_call),
                    coordinator.watching_inboxes(),
                ):
                    await stop.wait()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="teamup-client")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True, type=Path)
    sign_in = commands.add_parser("login")
    sign_in.add_argument("--config", required=True, type=Path)
    sign_in.add_argument("--username", required=True)
    commands.add_parser("init-binding")
    notifications = commands.add_parser("notifications")
    notifications.add_argument("--after", type=int)
    notifications.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    try:
        if args.command == "init-binding":
            print(
                json.dumps(
                    {
                        "binding_key": secrets.token_urlsafe(32),
                        "member_token": secrets.token_urlsafe(32),
                        "harness_instance_id": "edit-harness-instance-id",
                        "session_key": "edit-session-key",
                    },
                    indent=2,
                )
            )
        elif args.command == "serve":
            with suppress(KeyboardInterrupt):
                asyncio.run(_serve(args.config))
        elif args.command == "login":
            login(args.config, args.username)
            print("TeamUp login saved.")
        else:
            socket_path = os.getenv("TEAMUP_SOCKET")
            binding_key = os.getenv("TEAMUP_BINDING_KEY")
            if not socket_path or not binding_key:
                raise ConfigError("Set TEAMUP_SOCKET and TEAMUP_BINDING_KEY")
            params = {
                key: value
                for key, value in {"after": args.after, "limit": args.limit}.items()
                if value is not None
            }
            result = asyncio.run(
                call(socket_path, binding_key, "notifications", params)
            )
            print(json.dumps(result, ensure_ascii=False))
    except (ConfigError, LocalError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ClientError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": exc.message}}),
            file=sys.stderr,
        )
        return 1
    except (OSError, sqlite3.Error):
        print(
            "Could not access the Client socket or notification storage",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
