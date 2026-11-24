"""Production composition root for the TeamUp server."""

import argparse
import json
import os
import socket
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .collaboration import Collaboration
from .gateway import Gateway


def create_app(db_path: str | Path, enrollment_keys: Mapping[str, str]) -> FastAPI:
    return _compose(db_path, enrollment_keys).app


def _compose(db_path: str | Path, enrollment_keys: Mapping[str, str]) -> Gateway:
    return Gateway(Collaboration(db_path, enrollment_keys))


class _Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, gateway: Gateway):
        super().__init__(config)
        self._gateway = gateway

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
        keys = json.loads(os.environ.get("TEAMUP_ENROLLMENT_KEYS", ""))
    except ValueError:
        raise ValueError("TEAMUP_ENROLLMENT_KEYS must be a JSON object") from None
    if isinstance(keys, dict):
        return keys
    raise ValueError("TEAMUP_ENROLLMENT_KEYS must be a JSON object")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TeamUp server")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key are both required to enable TLS")
    host = "127.0.0.1" if args.host.lower() == "localhost" else args.host
    if not _loopback(host) and not args.tls_cert:
        parser.error("Non-loopback listeners require TLS")
    try:
        gateway = _compose(args.db, _enrollment_keys())
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
    _Server(config, gateway).run()


if __name__ == "__main__":
    main()
