"""Composition and command-line entry points for TeamUp Client."""

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import sqlite3
import sys
from contextlib import AsyncExitStack, nullcontext, suppress
from functools import partial
from pathlib import Path

from ._config import (
    ConfigError,
    locked_config,
    owner_key,
    read_config,
    read_locked_profile,
)
from ._console import serve_console
from ._console_data import ConsoleReader
from ._coordinator import Coordinator
from ._daemon import handle
from ._desktop import DesktopNotifier, default_command
from ._harness import default_codex_command, drivers
from ._local import LocalError, listen
from ._login import login, sign_in_profile
from ._notice_output import NoticeOutput
from ._notices import open_store
from ._protocol import is_string, is_uuid
from ._session_hook import register_session
from ._upstream import connect
from .ipc import ClientError, call


async def _activate_client(runtime, config, *, signed_in_console=False):
    async with AsyncExitStack() as startup:
        upstream = await startup.enter_async_context(connect(config))
        notices = startup.enter_context(open_store(config.socket_path))
        command = config.notify_command
        notifier = DesktopNotifier(default_command() if command is None else command)
        codex = config.codex_command
        coordinator = Coordinator(
            config,
            upstream,
            notices,
            NoticeOutput(),
            notifier,
            drivers(default_codex_command() if codex is None else codex),
            signed_in_console=signed_in_console,
        )
        await startup.enter_async_context(
            listen(config.socket_path, partial(handle, coordinator=coordinator))
        )
        await startup.enter_async_context(coordinator.watching_inboxes())
        runtime.push_async_exit(startup.pop_all())
        return ConsoleReader(coordinator.console_sessions, upstream.request, coordinator)


async def _serve(path, *, console=False, desktop=False):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        with locked_config(path, remove_stale_socket=True) as (config, _, _):
            async with AsyncExitStack() as runtime:

                async def sign_in(username, password, remember_scope):
                    profile = read_locked_profile(path, config.socket_path)
                    if remember_scope is not None and profile[0].remember_scope != remember_scope:
                        raise ConfigError("Server transport changed before sign-in")
                    async with connect(profile[0]) as upstream:
                        updated = await sign_in_profile(
                            path, profile, upstream.login, username, password
                        )
                    reader = await _activate_client(runtime, updated, signed_in_console=True)
                    return reader, updated.remember_scope

                needs_login = console and config.enrollment_key is None
                reader = None if needs_login else await _activate_client(runtime, config)
                viewer = (
                    serve_console(
                        reader,
                        sign_in=sign_in if needs_login else None,
                        desktop=desktop,
                        remember_scope=config.remember_scope,
                    )
                    if console
                    else nullcontext()
                )
                async with viewer as console_url:
                    if console_url:
                        print(console_url, flush=True)
                    await stop.wait()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def _owner(args):
    config = read_config(args.config)
    key = owner_key(config)
    if key is None:
        raise ConfigError("Owner commands need account_token or enrollment_key")
    if args.command == "wake":
        method, params = (
            "set_wake",
            {
                "harness_instance_id": args.harness_instance_id,
                "session_key": args.session_key,
                "rooms": args.rooms,
                "senders": args.senders,
            },
        )
    elif args.decision is None:
        method, params = "sessions", {}
    else:
        if args.session_key is None:
            raise ConfigError("Name the harness instance and session")
        params = {
            "harness_instance_id": args.harness_instance_id,
            "session_key": args.session_key,
        }
        if args.decision == "leave":
            if not is_uuid(args.room_id):
                raise ConfigError("Name the room to leave using its UUID")
            method = "leave_session_room"
            params["room_id"] = args.room_id
        elif args.room_id is not None:
            raise ConfigError("Only the leave command accepts a room ID")
        elif args.decision == "retire":
            method = "retire_session"
        else:
            method = "decide_session"
            params["state"] = {"approve": "approved", "reject": "rejected"}[args.decision]
    return await call(config.socket_path, key, method, params)


async def _manage_member(args):
    config = read_config(args.config)
    if config.account_token is None:
        raise ConfigError("Run teamup-client login to manage members")
    if not is_uuid(args.member_id):
        raise ConfigError("Member ID must be a UUID")
    path = f"/members/{args.member_id}"
    async with connect(config) as upstream:
        if args.action == "retire":
            return await upstream.request(
                "POST", f"{path}/retire", config.account_token, member_auth=False
            )
        if args.action == "room-responsibility":
            if not is_uuid(args.room_id) or not re.fullmatch(r"[0-9a-f]{32}", args.membership_id):
                raise ConfigError("Provide the room UUID and its current membership ID")
            return await upstream.request(
                "PATCH",
                f"/rooms/{args.room_id}/members/{args.member_id}/responsibility",
                config.account_token,
                body={"membership_id": args.membership_id, "description": args.description},
                member_auth=False,
            )
        return await upstream.request(
            "PATCH",
            path,
            config.account_token,
            body={"role_description": args.role_description},
            member_auth=False,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="teamup-client")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True, type=Path)
    serve.add_argument(
        "--console", action="store_true", help="Start the local console with interactive sign-in"
    )
    serve.add_argument("--desktop-console", action="store_true", help=argparse.SUPPRESS)
    sign_in = commands.add_parser("login")
    sign_in.add_argument("--config", required=True, type=Path)
    sign_in.add_argument("--username", required=True)
    members = commands.add_parser(
        "members", help="Manage members owned by your account"
    )
    members.add_argument("--config", required=True, type=Path)
    member_actions = members.add_subparsers(dest="action", required=True)
    rename = member_actions.add_parser(
        "rename", help="Update a member's role description"
    )
    rename.add_argument("member_id")
    rename.add_argument("role_description")
    responsibility = member_actions.add_parser(
        "room-responsibility", help="Set a member's responsibility for one room"
    )
    responsibility.add_argument("member_id")
    responsibility.add_argument("room_id")
    responsibility.add_argument("membership_id")
    setting = responsibility.add_mutually_exclusive_group(required=True)
    setting.add_argument("--description")
    setting.add_argument("--clear", dest="description", action="store_const", const=None)
    retire = member_actions.add_parser("retire", help="Permanently retire a member")
    retire.add_argument("member_id")
    commands.add_parser("init-binding")
    install = commands.add_parser(
        "init-install",
        help="Generate a key that identifies sessions within a harness install",
    )
    install.add_argument("harness_instance_id")
    for name in ("codex", "claude"):
        hook = commands.add_parser(
            f"{name}-hook", help=f"Register a local {name.capitalize()} session"
        )
        hook.add_argument("--config", required=True, type=Path)
        hook.add_argument("--install", required=True)
        hook.add_argument("--workspace", type=Path, help="Absolute collaboration workspace")
    sessions = commands.add_parser("sessions", help="List and manage agent sessions")
    sessions.add_argument("--config", required=True, type=Path)
    sessions.add_argument("decision", nargs="?", choices=("approve", "reject", "leave", "retire"))
    sessions.add_argument("harness_instance_id", nargs="?")
    sessions.add_argument("session_key", nargs="?")
    sessions.add_argument("room_id", nargs="?")
    wake = commands.add_parser(
        "wake", help="Allow mentions to start turns in one session"
    )
    wake.add_argument("--config", required=True, type=Path)
    wake.add_argument("harness_instance_id")
    wake.add_argument("session_key")
    wake.add_argument("--room", action="append", default=[], dest="rooms")
    wake.add_argument("--sender", action="append", default=[], dest="senders")
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
        elif args.command == "init-install":
            if not is_string(args.harness_instance_id, 128):
                raise ConfigError(
                    "The harness instance ID must contain 1–128 characters"
                )
            print(
                json.dumps(
                    {
                        "binding_key": secrets.token_urlsafe(32),
                        "harness_instance_id": args.harness_instance_id,
                    },
                    indent=2,
                )
            )
        elif args.command in ("codex-hook", "claude-hook"):
            kind = "codex" if args.command == "codex-hook" else "claude-code"
            asyncio.run(
                register_session(
                    args.config, args.install, sys.stdin.buffer,
                    kind=kind, workspace=args.workspace,
                )
            )
        elif args.command in ("sessions", "wake"):
            print(json.dumps(asyncio.run(_owner(args)), ensure_ascii=False, indent=2))
        elif args.command == "members":
            updated = asyncio.run(_manage_member(args))
            print(json.dumps(updated, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            with suppress(KeyboardInterrupt):
                asyncio.run(_serve(
                    args.config,
                    console=args.console or args.desktop_console,
                    desktop=args.desktop_console,
                ))
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
