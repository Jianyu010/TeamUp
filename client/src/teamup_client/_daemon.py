import asyncio
import sqlite3
from contextlib import suppress

from ._local import same_user
from ._protocol import REQUEST_LIMIT, RESPONSE_LIMIT, decode_json, frame
from .ipc import ClientError


def _request(payload):
    if len(payload) > REQUEST_LIMIT or not payload.endswith(b"\n"):
        raise ValueError
    request = decode_json(payload)
    if not (
        isinstance(request, dict)
        and set(request) == {"version", "binding_key", "method", "params"}
        and type(request["version"]) is int
        and request["version"] == 1
        and isinstance(request["binding_key"], str)
        and isinstance(request["method"], str)
        and isinstance(request["params"], dict)
    ):
        raise ValueError
    return request["binding_key"], request["method"], request["params"]


def _error(code, message):
    return {"error": {"code": code, "message": message}}


async def handle(reader, writer, coordinator):
    try:
        try:
            if not same_user(writer.get_extra_info("socket")):
                raise ClientError("unauthorized", "Unexpected socket peer")
            async with asyncio.timeout(5):
                request = _request(await reader.readline())
            response = {"result": await coordinator.execute(*request)}
        except ClientError as exc:
            response = _error(exc.code, exc.message)
        except (ValueError, TimeoutError, RecursionError):
            response = _error("invalid_request", "Invalid IPC request")
        except sqlite3.Error:
            response = _error(
                "unavailable", "Local notification storage is unavailable"
            )
        try:
            payload = frame(response)
        except (ValueError, RecursionError):
            payload = frame(_error("unavailable", "Invalid response data"))
        if len(payload) > RESPONSE_LIMIT:
            payload = frame(_error("unavailable", "Response exceeds the IPC limit"))
        writer.write(payload)
        async with asyncio.timeout(5):
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
