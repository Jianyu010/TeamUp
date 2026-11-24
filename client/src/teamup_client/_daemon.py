import asyncio
import sqlite3
from contextlib import suppress

from ._coordinator import valid_session
from ._local import same_user
from ._protocol import REQUEST_LIMIT, RESPONSE_LIMIT, decode_json, frame
from .ipc import ClientError


def _request(payload):
    if len(payload) > REQUEST_LIMIT or not payload.endswith(b"\n"):
        raise ValueError
    request = decode_json(payload)
    if not (
        isinstance(request, dict)
        and {"version", "binding_key", "method", "params"}
        <= set(request)
        <= {"version", "binding_key", "method", "params", "session"}
        and valid_session(request.get("session"))
        and type(request["version"]) is int
        and request["version"] == 1
        and isinstance(request["binding_key"], str)
        and isinstance(request["method"], str)
        and isinstance(request["params"], dict)
    ):
        raise ValueError
    return (
        request["binding_key"],
        request["method"],
        request["params"],
        request.get("session"),
    )


def _error(code, message):
    return {"error": {"code": code, "message": message}}


async def handle(reader, writer, coordinator):
    release = None
    try:
        try:
            if not same_user(writer.get_extra_info("socket")):
                raise ClientError("unauthorized", "Unexpected socket peer")
            async with asyncio.timeout(5):
                key, method, params, session = _request(await reader.readline())
            if method == "attach":
                release = coordinator.attach(key, session, params)
                response = {"result": {"attached": True}}
            else:
                response = {
                    "result": await coordinator.execute(key, method, params, session)
                }
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
        if release is not None:
            # The lease lasts while the attached MCP process keeps this open.
            while await reader.read(4096):
                pass
    except (OSError, TimeoutError):
        pass
    finally:
        if release is not None:
            release()
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
