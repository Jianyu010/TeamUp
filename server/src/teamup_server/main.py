"""Production composition root for the TeamUp server."""

import argparse
import json
import os
import socket
import sys
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import account_cli
from .collaboration import Collaboration
from .gateway import Gateway
from .maintenance import MAX_RETENTION_HOURS, Maintenance, retention


def create_app(
    db_path: str | Path, enrollment_keys: Mapping[str, str] | None = None
) -> FastAPI:
    return _compose(db_path, enrollment_keys or {}, None, None)[0].app


def _compose(
    db_path: str | Path,
    enrollment_keys: Mapping[str, str],
    room_retention_hours: int | None,
    dm_retention_hours: int | None,
) -> tuple[Gateway, Maintenance | None]:
    collaboration = Collaboration(db_path, enrollment_keys)
    return Gateway(collaboration), retention(
        collaboration, room_retention_hours, dm_retention_hours
    )


class _Server(uvicorn.Server):
    def __init__(
        self,
        config: uvicorn.Config,
        gateway: Gateway,
        maintenance: Maintenance | None,
    ):
        super().__init__(config)
        self._gateway = gateway
        self._maintenance = maintenance

    def run(self, sockets: list[socket.socket] | None = None) -> None:
        if self._maintenance is None:
            super().run(sockets=sockets)
        else:
            with self._maintenance:
                super().run(sockets=sockets)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._gateway.stop_streams()
        await super().shutdown(sockets=sockets)


def _loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _enrollment_keys() -> dict[str, str]:
    try:
        keys = json.loads(os.environ.get("TEAMUP_ENROLLMENT_KEYS", "{}"))
    except ValueError:
        raise ValueError("TEAMUP_ENROLLMENT_KEYS must be a JSON object") from None
    if isinstance(keys, dict):
        return keys
    raise ValueError("TEAMUP_ENROLLMENT_KEYS must be a JSON object")


def main() -> None:
    if sys.argv[1:2] == ["accounts"]:
        parser = account_cli.parser()
        args = parser.parse_args(sys.argv[2:])
        try:
            account_cli.run(args, Collaboration(args.db, {}))
        except ValueError as exc:
            parser.error(str(exc))
        return

    parser = argparse.ArgumentParser(
        description="Run the TeamUp server",
        epilog="Use 'teamup-server accounts --help' to administer accounts.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--room-retention-hours", type=int)
    parser.add_argument("--dm-retention-hours", type=int)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.room_retention_hours is not None and not (
        1 <= args.room_retention_hours <= MAX_RETENTION_HOURS
    ):
        parser.error(
            f"--room-retention-hours must be an integer between 1 and {MAX_RETENTION_HOURS}"
        )
    if args.dm_retention_hours is not None and not (
        1 <= args.dm_retention_hours <= MAX_RETENTION_HOURS
    ):
        parser.error(
            f"--dm-retention-hours must be an integer between 1 and {MAX_RETENTION_HOURS}"
        )
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key are both required to enable TLS")
    host = "127.0.0.1" if args.host.lower() == "localhost" else args.host
    if not _loopback(host) and not args.tls_cert:
        parser.error("Non-loopback listeners require TLS")
    try:
        gateway, maintenance = _compose(
            args.db,
            _enrollment_keys(),
            args.room_retention_hours,
            args.dm_retention_hours,
        )
    except ValueError as exc:
        parser.error(str(exc))
    config = uvicorn.Config(
        gateway.app,
        host=host,
        port=args.port,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
        timeout_graceful_shutdown=5,
    )
    _Server(config, gateway, maintenance).run()


if __name__ == "__main__":
    main()
