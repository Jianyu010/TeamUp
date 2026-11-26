"""The public asynchronous TeamUp Client IPC entry point."""

import asyncio
from contextlib import asynccontextmanager, suppress
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


def _encode(socket_path, binding_key, method, params, session):
    try:
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError
        envelope = {
            "version": 1,
            "binding_key": binding_key,
            "method": method,
            "params": params,
        }
        if session is not None:
            envelope["session"] = session
        request = frame(envelope)
        if len(request) > REQUEST_LIMIT:
            raise ValueError
    except (TypeError, ValueError, RecursionError):
        raise ClientError(
            "invalid_request", "Invalid IPC request or socket path"
        ) from None
    return path, request


async def _exchange(path, request):
    private_directory(path.parent)
    private_socket(path)
    async with asyncio.timeout(15):
        reader, writer = await asyncio.open_unix_connection(path, limit=RESPONSE_LIMIT)
        try:
            if not same_user(writer.get_extra_info("socket")):
                raise LocalError("Unexpected socket peer")
            writer.write(request)
            await writer.drain()
            response = await reader.readline()
        except BaseException:
            writer.close()
            raise
    return response, reader, writer


async def call(
    socket_path: str | PathLike[str],
    binding_key: str,
    method: str,
    params: dict,
    session: dict | None = None,
) -> dict:
    """Call one TeamUp operation over the local Unix socket.

    ``session`` is ``{"id": ..., "kind": "claude-code" | "codex"}`` when the
    caller runs inside an identifiable harness session.
    """
    path, request = _encode(socket_path, binding_key, method, params, session)
    try:
        response, _, writer = await _exchange(path, request)
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        return _decode(response)
    except (OSError, LocalError, ValueError, RecursionError, TimeoutError):
        raise ClientError("unavailable", "TeamUp Client is unavailable") from None


@asynccontextmanager
async def attach(
    socket_path: str | PathLike[str],
    binding_key: str,
    session: dict,
    address: str,
    token: str,
):
    """Hold a live-session lease for as long as the context stays open.

    Yields an event that is set if the Client closes the connection.
    """
    path, request = _encode(
        socket_path,
        binding_key,
        "attach",
        {"address": address, "token": token},
        session,
    )
    try:
        response, reader, writer = await _exchange(path, request)
        _decode(response)
    except (OSError, LocalError, ValueError, RecursionError, TimeoutError):
        raise ClientError("unavailable", "TeamUp Client is unavailable") from None
    except ClientError:
        writer.close()
        raise
    closed = asyncio.Event()

    async def watch():
        with suppress(OSError):
            await reader.read()
        closed.set()

    watcher = asyncio.create_task(watch())
    try:
        yield closed
    finally:
        watcher.cancel()
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def _decode(response):
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
