import re
from contextlib import asynccontextmanager

import httpx

from ._protocol import decode_json, is_integer, is_secret
from .ipc import ClientError

ERROR_MESSAGES = {
    "invalid_request": "Server rejected the request parameters",
    "unauthorized": "Server rejected the credentials",
    "not_found": "Resource missing or inaccessible",
    "board_read_required": "Read the current board before acknowledging it",
    "board_ack_required": "Confirm the current board before exchanging messages",
    "conflict": "Request conflicts with existing server state",
    "membership_changed": "This session's room membership has changed",
    "member_retired": "This member's binding has been retired",
    "invalid_mention": "Mention must target another member of the room",
    "invalid_peer": "DM peer must be another available member of this team",
    "rate_limited": "Too many login attempts. Try again later",
}


def unavailable():
    return ClientError("unavailable", "TeamUp Server is unavailable")


def _status_error(response, *, member_auth):
    if response.status_code >= 500 or response.status_code < 400:
        return unavailable()
    if member_auth and response.status_code == 401:
        return ClientError(
            "not_registered", "This binding has not registered with TeamUp"
        )
    try:
        code = decode_json(response.content)["error"]["code"]
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            return ClientError(
                code, ERROR_MESSAGES.get(code, "Server rejected the request")
            )
    except (KeyError, TypeError, ValueError, RecursionError):
        pass
    return unavailable()


@asynccontextmanager
async def connect(config):
    async with httpx.AsyncClient(
        base_url=config.server_url,
        verify=config.tls,
        timeout=10,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=20),
    ) as http:
        yield Upstream(http)


class Upstream:
    def __init__(self, http):
        self._http = http

    async def login(self, username, password):
        try:
            result = await self.request(
                "POST",
                "/auth/login",
                body={"username": username, "password": password},
                member_auth=False,
            )
        except ClientError as exc:
            if exc.code in {
                "unauthorized",
                "invalid_request",
                "rate_limited",
                "unavailable",
            }:
                raise
            raise unavailable() from None
        if (
            set(result) != {"access_token", "expires_at"}
            or not is_secret(result["access_token"])
            or not is_integer(result["expires_at"], 1)
        ):
            raise unavailable()
        return result["access_token"]

    async def request(
        self, method, path, token=None, *, query=None, body=None, member_auth=True
    ):
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._http.request(
                method,
                f"/v1{path}",
                headers=headers,
                params=query,
                json=body,
            )
        except httpx.HTTPError:
            raise unavailable() from None
        if response.status_code == 404 and method == "GET" and path == "/version":
            return {}  # Older Servers have no build metadata endpoint.
        if response.status_code != 200:
            raise _status_error(response, member_auth=member_auth)
        try:
            result = decode_json(response.content)
        except (ValueError, RecursionError):
            raise unavailable() from None
        if not isinstance(result, dict):
            raise unavailable()
        return result

    @asynccontextmanager
    async def events(self, token):
        try:
            async with self._http.stream(
                "GET",
                "/v1/events",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
                timeout=httpx.Timeout(10, read=45),
            ) as response:
                if response.status_code != 200:
                    # The worker can retry all stream failures without accessing an error body.
                    raise unavailable()
                if (
                    response.headers.get("content-type", "").split(";", 1)[0]
                    != "text/event-stream"
                ):
                    raise unavailable()
                yield response.aiter_lines()
        except httpx.HTTPError:
            raise unavailable() from None
