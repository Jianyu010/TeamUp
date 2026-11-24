"""The public asynchronous TeamUp Client IPC entry point."""

import asyncio
from contextlib import suppress
from os import PathLike
from pathlib import Path

from ._local import LocalError, private_directory, private_socket, same_user
from ._protocol import REQUEST_LIMIT, RESPONSE_LIMIT, decode_json, frame


class ClientError(Exception):
    """A failed TeamUp Client call."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def call(
    socket_path: str | PathLike[str],
    binding_key: str,
    method: str,
    params: dict,
) -> dict:
    """Call one TeamUp operation over the local Unix socket."""
    try:
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError
        request = frame(
            {
                "version": 1,
                "binding_key": binding_key,
                "method": method,
                "params": params,
            }
        )
        if len(request) > REQUEST_LIMIT:
            raise ValueError
    except (TypeError, ValueError, RecursionError):
        raise ClientError(
            "invalid_request", "Invalid IPC request or socket path"
        ) from None

    try:
        private_directory(path.parent)
        private_socket(path)
        async with asyncio.timeout(15):
            reader, writer = await asyncio.open_unix_connection(
                path, limit=RESPONSE_LIMIT
            )
            try:
                if not same_user(writer.get_extra_info("socket")):
                    raise LocalError("Unexpected socket peer")
                writer.write(request)
                await writer.drain()
                response = await reader.readline()
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
        if len(response) > RESPONSE_LIMIT or not response.endswith(b"\n"):
            raise ValueError
        payload = decode_json(response)
        if isinstance(payload, dict):
            if set(payload) == {"result"} and isinstance(payload["result"], dict):
                return payload["result"]
            if set(payload) == {"error"} and isinstance(payload["error"], dict):
                code, message = (
                    payload["error"].get("code"),
                    payload["error"].get("message"),
                )
                if isinstance(code, str) and isinstance(message, str):
                    raise ClientError(code, message)
        raise ValueError
    except (OSError, LocalError, ValueError, RecursionError):
        raise ClientError("unavailable", "TeamUp Client is unavailable") from None
