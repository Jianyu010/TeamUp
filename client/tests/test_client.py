import asyncio
import getpass
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections import Counter
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ENROLLMENT = "A" * 43
ACCOUNT_TOKEN = "F" * 42 + "A"
ACCOUNT_PASSWORD = "  correctly spelled password  "
BINDINGS = [
    {
        "binding_key": "B" * 42 + "A",
        "member_token": "C" * 42 + "A",
        "harness_instance_id": "harness",
        "session_key": "session-a",
    },
    {
        "binding_key": "D" * 42 + "A",
        "member_token": "E" * 42 + "A",
        "harness_instance_id": "harness",
        "session_key": "session-b",
    },
]
MEMBERS = [
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
]
ROOM = "00000000-0000-4000-8000-000000000003"
CLI = (sys.executable, "-m", "teamup_client.main")
RECORDER = (
    "import json, sys, time\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as log:\n"
    "    log.write(json.dumps([time.time(), *sys.argv[2:]]) + '\\n')\n"
    "sys.exit(int(sys.argv[2] == 'fail mentioned you'))\n"
)


def recorder(path):
    return [sys.executable, "-c", RECORDER, str(path)]


def recorded(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class TeamupGateway:
    def __init__(self):
        self.registered = {}
        self.calls = []
        self.events = {}
        self.ack_failures = Counter()
        self.streams = {}
        self.account_valid = True

    async def respond(self, reader, writer):
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, _ = request_line.decode().split()
            headers = {}
            while line := (await reader.readline()).strip():
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            length = int(headers.get("content-length", 0))
            body = json.loads(await reader.readexactly(length)) if length else None
            authorization = headers.get("authorization")
            token = authorization.removeprefix("Bearer ") if authorization else None
            url = urlsplit(target)
            path = url.path
            query = parse_qs(url.query, keep_blank_values=True)
            self.calls.append((method, path, query, token, body))

            if path == "/v1/auth/login" and method == "POST":
                if body == {"username": "alice", "password": ACCOUNT_PASSWORD}:
                    await self.json_response(
                        writer,
                        {"access_token": ACCOUNT_TOKEN, "expires_at": 1780000000},
                    )
                else:
                    await self.json_response(
                        writer,
                        {
                            "error": {
                                "code": "unauthorized",
                                "message": body["password"],
                            }
                        },
                        401,
                    )
            elif (
                path == "/v1/members"
                and method == "POST"
                and (
                    token == ENROLLMENT
                    or (token == ACCOUNT_TOKEN and self.account_valid)
                )
            ):
                binding_index = [item["session_key"] for item in BINDINGS].index(
                    body["session_key"]
                )
                member = self.registered.setdefault(
                    body["member_token"],
                    {
                        "member_id": MEMBERS[binding_index],
                        "role_description": body["role_description"],
                    },
                )
                await self.json_response(writer, member)
            elif token not in self.registered:
                await self.json_response(
                    writer,
                    {"error": {"code": "unauthorized", "message": "Unknown member"}},
                    401,
                )
            elif path == "/v1/members/me":
                await self.json_response(writer, self.registered[token])
            elif path == "/v1/members":
                await self.json_response(
                    writer, {"members": list(self.registered.values())}
                )
            elif path == "/v1/events":
                assert headers["accept"] == "text/event-stream"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n: ready\n\n"
                )
                await writer.drain()
                self.streams[token] = writer
                await reader.read()
            elif path == "/v1/inbox":
                after = int(query.get("after", [0])[0])
                limit = int(query.get("limit", [50])[0])
                events = [
                    event
                    for event_id, event in sorted(self.events.get(token, {}).items())
                    if event_id > after
                ][:limit]
                await self.json_response(writer, {"events": events})
            elif path.startswith("/v1/inbox/"):
                event_id = int(path.split("/")[-2])
                if self.ack_failures[event_id]:
                    self.ack_failures[event_id] -= 1
                    await self.json_response(writer, {}, 503)
                else:
                    self.events[token].pop(event_id, None)
                    await self.json_response(writer, {"event_id": event_id})
            elif path == f"/v1/rooms/{ROOM}/messages" and method == "POST":
                await self.json_response(
                    writer,
                    {
                        "error": {
                            "code": "board_ack_required",
                            "message": "Confirm the board",
                        }
                    },
                    409,
                )
            else:
                await self.json_response(writer, {"room_id": ROOM, "name": "team"})
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    @staticmethod
    async def json_response(writer, payload, status=200):
        body = json.dumps(payload).encode()
        writer.write(
            f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()


@asynccontextmanager
async def gateway():
    upstream = TeamupGateway()
    server = await asyncio.start_server(upstream.respond, "127.0.0.1", 0)
    async with server:
        yield upstream, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def cli(*args, env=None, stderr=asyncio.subprocess.PIPE):
    env = {
        **(os.environ if env is None else env),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    return await asyncio.create_subprocess_exec(
        *CLI,
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    )


def write_config(directory, server_url, **overrides):
    config = directory / "config.json"
    socket = directory / "teamup.sock"
    config.write_text(
        json.dumps(
            {
                "server_url": server_url,
                "socket_path": str(socket),
                "enrollment_key": ENROLLMENT,
                "bindings": BINDINGS,
                # Tests never raise real desktop notices.
                "notify_command": [],
                **overrides,
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config, socket


async def until(condition):
    async with asyncio.timeout(8):
        while not condition():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def daemon(config, socket, *, stderr=asyncio.subprocess.PIPE):
    process = await cli("serve", "--config", str(config), stderr=stderr)
    try:
        await until(lambda: socket.exists() or process.returncode is not None)
        if process.returncode is not None:
            raise AssertionError(
                await process.stderr.read() if process.stderr else process.returncode
            )
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)


async def raw_request(socket, payload):
    reader, writer = await asyncio.open_unix_connection(socket)
    try:
        writer.write(payload)
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


def test_init_binding_and_reject_untrusted_config():
    async def scenario():
        generated = []
        for _ in range(2):
            process = await cli("init-binding")
            stdout, stderr = await process.communicate()
            assert process.returncode == 0 and stderr == b""
            binding = json.loads(stdout)
            assert set(binding) == set(BINDINGS[0])
            for key in ("binding_key", "member_token"):
                assert re.fullmatch("[A-Za-z0-9_-]{43}", binding[key])
                generated.append(binding[key])
        assert len(set(generated)) == 4

        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            directory = Path(name)
            for url, mode in (
                ("http://192.0.2.1", 0o600),
                ("https://localhost", 0o644),
            ):
                config, socket = write_config(directory, url)
                config.chmod(mode)
                process = await cli("serve", "--config", str(config))
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode != 0 and stdout == b"" and stderr
                assert ENROLLMENT.encode() not in stderr
                assert not socket.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_login_atomically_saves_a_private_account_token(legacy, monkeypatch, capsys):
    from teamup_client.main import main

    monkeypatch.setattr(getpass, "getpass", lambda prompt: ACCOUNT_PASSWORD)

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, _ = write_config(Path(name), server_url)
                original = json.loads(config.read_bytes())
                if not legacy:
                    del original["enrollment_key"]
                    config.write_text(json.dumps(original), encoding="utf-8")
                config.chmod(0o400)
                with config.open("rb") as previous:
                    result = await asyncio.to_thread(
                        main, ["login", "--config", str(config), "--username", "alice"]
                    )
                    assert result == 0
                    assert json.load(previous) == original
                    assert config.stat().st_ino != os.fstat(previous.fileno()).st_ino

                original.pop("enrollment_key", None)
                assert json.loads(config.read_bytes()) == {
                    **original,
                    "account_token": ACCOUNT_TOKEN,
                }
                assert stat.S_IMODE(config.stat().st_mode) == 0o600
                assert upstream.calls == [
                    (
                        "POST",
                        "/v1/auth/login",
                        {},
                        None,
                        {"username": "alice", "password": ACCOUNT_PASSWORD},
                    )
                ]

    asyncio.run(scenario())
    output = capsys.readouterr()
    assert output.out and not output.err
    assert ACCOUNT_PASSWORD not in output.out and ACCOUNT_TOKEN not in output.out


def test_failed_login_preserves_the_configuration(monkeypatch, capsys):
    from teamup_client.main import main

    password = "incorrect password"
    monkeypatch.setattr(getpass, "getpass", lambda prompt: password)

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, _ = write_config(Path(name), server_url)
                original = config.read_bytes()
                result = await asyncio.to_thread(
                    main, ["login", "--config", str(config), "--username", "alice"]
                )
                assert result == 1
                assert config.read_bytes() == original
                assert len(upstream.calls) == 1

    asyncio.run(scenario())
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "unauthorized"
    assert password not in output.err


@pytest.mark.parametrize("phase", ["prompt", "response"])
def test_login_preserves_edits_made_during_authentication(phase, monkeypatch, capsys):
    from teamup_client.main import main

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, _ = write_config(Path(name), server_url)
                updated = {
                    **json.loads(config.read_bytes()),
                    "server_url": "https://localhost:9443",
                    "ca_file": "replacement-ca.pem",
                    "bindings": [],
                }
                edited_config = json.dumps(updated, indent=4)

                def prompt(_prompt):
                    if phase == "prompt":
                        config.write_text(edited_config, encoding="utf-8")
                    return ACCOUNT_PASSWORD

                async def respond(writer, payload, status=200):
                    config.write_text(edited_config, encoding="utf-8")
                    await TeamupGateway.json_response(writer, payload, status)

                monkeypatch.setattr(getpass, "getpass", prompt)
                if phase == "response":
                    monkeypatch.setattr(upstream, "json_response", respond)

                result = await asyncio.to_thread(
                    main, ["login", "--config", str(config), "--username", "alice"]
                )
                assert config.read_text(encoding="utf-8") == edited_config
                assert result == 1
                assert len(upstream.calls) == (0 if phase == "prompt" else 1)

    asyncio.run(scenario())
    output = capsys.readouterr()
    assert not output.out
    assert "configuration changed" in output.err
    assert ACCOUNT_PASSWORD not in output.err and ACCOUNT_TOKEN not in output.err


def test_login_requires_the_client_to_stop(monkeypatch, capsys):
    from teamup_client.main import main

    def unexpected_prompt(prompt):
        pytest.fail("Login prompted for a password while the Client was running")

    monkeypatch.setattr(getpass, "getpass", unexpected_prompt)

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (_, server_url):
                config, socket = write_config(Path(name), server_url)
                original = config.read_bytes()
                async with daemon(config, socket):
                    result = await asyncio.to_thread(
                        main, ["login", "--config", str(config), "--username", "alice"]
                    )
                    assert result == 1
                    assert config.read_bytes() == original
                    assert socket.exists()

    asyncio.run(scenario())
    assert "already in use" in capsys.readouterr().err


def test_account_registration_reuses_the_member_after_login_expires():
    from teamup_client.ipc import ClientError, call

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, socket = write_config(Path(name), server_url)
                settings = json.loads(config.read_bytes())
                del settings["enrollment_key"]
                settings["account_token"] = ACCOUNT_TOKEN
                config.write_text(json.dumps(settings), encoding="utf-8")
                key_a, key_b = (binding["binding_key"] for binding in BINDINGS)
                params = {"role_description": "developer"}

                async with daemon(config, socket):
                    member = await call(socket, key_a, "register", params)
                    assert member == {
                        "member_id": MEMBERS[0],
                        "role_description": "developer",
                    }
                assert [
                    entry[3]
                    for entry in upstream.calls
                    if entry[:2] == ("POST", "/v1/members")
                ] == [ACCOUNT_TOKEN]

                upstream.account_valid = False
                upstream.calls.clear()
                async with daemon(config, socket):
                    assert await call(socket, key_a, "register", params) == member
                    assert await call(socket, key_a, "find_members", {}) == {
                        "members": [member]
                    }
                    assert all(entry[3] != ACCOUNT_TOKEN for entry in upstream.calls)
                    with pytest.raises(ClientError) as error:
                        await call(socket, key_b, "register", params)
                    assert error.value.code == "unauthorized"

    asyncio.run(scenario())


def test_ipc_enforces_binding_and_maps_contract_operations():
    from teamup_client.ipc import ClientError, call

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, socket = write_config(Path(name), server_url)
                async with daemon(config, socket):
                    assert stat.S_IMODE(socket.stat().st_mode) == 0o600
                    token_a, token_b = (binding["member_token"] for binding in BINDINGS)
                    key_a, key_b = (binding["binding_key"] for binding in BINDINGS)
                    with pytest.raises(ClientError) as error:
                        await call(socket, key_a, "find_members", {})
                    assert error.value.code == "not_registered"
                    for method, params, key, code in [
                        ("find_members", {}, ENROLLMENT, "unauthorized"),
                        (
                            "find_members",
                            {"member_token": token_b},
                            key_a,
                            "invalid_request",
                        ),
                        (
                            "read_messages",
                            {"room_id": ROOM, "after": True},
                            key_a,
                            "invalid_request",
                        ),
                        ("nonsense", {}, key_a, "invalid_request"),
                    ]:
                        with pytest.raises(ClientError) as error:
                            await call(socket, key, method, params)
                        assert error.value.code == code
                        assert token_b not in error.value.message

                    response = await raw_request(socket, b'{"version":true}\n')
                    assert response["error"]["code"] == "invalid_request"
                    response = await raw_request(socket, b"x" * (128 * 1024) + b"\n")
                    assert response["error"]["code"] == "invalid_request"

                    for key, member_id in zip((key_a, key_b), MEMBERS, strict=True):
                        assert await call(
                            socket, key, "register", {"role_description": "dev"}
                        ) == {
                            "member_id": member_id,
                            "role_description": "dev",
                        }
                    assert await call(socket, key_a, "find_members", {"q": "dev"}) == {
                        "members": list(upstream.registered.values())
                    }
                    assert (
                        "GET",
                        "/v1/members",
                        {"q": ["dev"]},
                        token_a,
                        None,
                    ) in upstream.calls

                    cases = [
                        ("list_rooms", {}, "GET", "/v1/rooms", {}, None),
                        (
                            "create_room",
                            {"name": "team"},
                            "POST",
                            "/v1/rooms",
                            {},
                            {"name": "team"},
                        ),
                        (
                            "join_room",
                            {"room_id": ROOM},
                            "POST",
                            f"/v1/rooms/{ROOM}/join",
                            {},
                            None,
                        ),
                        (
                            "get_board",
                            {"room_id": ROOM},
                            "GET",
                            f"/v1/rooms/{ROOM}/board",
                            {},
                            None,
                        ),
                        (
                            "ack_board",
                            {"room_id": ROOM, "version": 1},
                            "POST",
                            f"/v1/rooms/{ROOM}/board/ack",
                            {},
                            {"version": 1},
                        ),
                        (
                            "read_messages",
                            {"room_id": ROOM, "after": 0, "limit": 2},
                            "GET",
                            f"/v1/rooms/{ROOM}/messages",
                            {"after": ["0"], "limit": ["2"]},
                            None,
                        ),
                    ]
                    for method, params, verb, route, query, body in cases:
                        await call(socket, key_b, method, params)
                        assert (verb, route, query, token_b, body) in upstream.calls
                    with pytest.raises(ClientError) as error:
                        await call(
                            socket,
                            key_a,
                            "send_message",
                            {
                                "room_id": ROOM,
                                "text": "hello",
                                "client_message_id": "id",
                                "mention_member_id": MEMBERS[1],
                            },
                        )
                    assert error.value.code == "board_ack_required"
                    assert (
                        "POST",
                        f"/v1/rooms/{ROOM}/messages",
                        {},
                        token_a,
                        {
                            "text": "hello",
                            "client_message_id": "id",
                            "mention_member_id": MEMBERS[1],
                        },
                    ) in upstream.calls
                assert not socket.exists()

    asyncio.run(scenario())


def test_sse_backfill_persists_and_deduplicates_mentions_across_restarts():
    from teamup_client.ipc import call

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, socket = write_config(Path(name), server_url)
                token_a, token_b = (binding["member_token"] for binding in BINDINGS)
                key_a, key_b = (binding["binding_key"] for binding in BINDINGS)
                upstream.registered = {
                    token: {"member_id": member, "role_description": "dev"}
                    for token, member in zip((token_a, token_b), MEMBERS, strict=True)
                }
                mention = {
                    "event_id": 8,
                    "kind": "mention",
                    "message": {
                        "message_id": 7,
                        "room_id": ROOM,
                        "sender_member_id": MEMBERS[1],
                        "client_message_id": "offline",
                        "text": "private message",
                        "mention_member_id": MEMBERS[0],
                    },
                }
                plain = {
                    "event_id": 9,
                    "kind": "message",
                    "message": {
                        **mention["message"],
                        "message_id": 8,
                        "mention_member_id": None,
                    },
                }
                notice = {
                    "event_id": 8,
                    "room_id": ROOM,
                    "message_id": 7,
                    "sender_member_id": MEMBERS[1],
                }
                # The first acknowledgment fails after the daemon persists the notice.
                upstream.events[token_a] = {8: mention, 9: plain}
                upstream.ack_failures[8] = 50
                async with daemon(config, socket) as first:
                    printed = await asyncio.wait_for(first.stderr.readline(), 5)
                    assert json.loads(printed) == notice
                    assert await call(socket, key_a, "notifications", {}) == {
                        "notifications": [notice]
                    }
                    assert await call(socket, key_b, "notifications", {}) == {
                        "notifications": []
                    }
                    assert upstream.events[token_a][8] == mention
                    first.kill()
                    await first.wait()
                assert socket.exists()

                upstream.ack_failures[8] = 0
                async with daemon(config, socket) as second:
                    await until(lambda: not upstream.events[token_a])
                    live = {
                        **mention,
                        "event_id": 10,
                        "message": {**mention["message"], "message_id": 9},
                    }
                    live_notice = {**notice, "event_id": 10, "message_id": 9}
                    upstream.events[token_a][10] = live
                    stream = upstream.streams[token_a]
                    stream.write(b"event: inbox_changed\ndata: {}\n\n")
                    await stream.drain()
                    printed = await asyncio.wait_for(second.stderr.readline(), 5)
                    assert json.loads(printed) == live_notice
                    await until(lambda: not upstream.events[token_a])
                    env = {
                        **os.environ,
                        "TEAMUP_SOCKET": str(socket),
                        "TEAMUP_BINDING_KEY": key_a,
                    }
                    process = await cli(
                        "notifications", "--after", "8", "--limit", "1", env=env
                    )
                    stdout, stderr = await process.communicate()
                    assert process.returncode == 0 and stderr == b""
                    assert json.loads(stdout) == {"notifications": [live_notice]}

                    # Reopen SSE and backfill even though no hint was sent.
                    await until(lambda: not upstream.events[token_a])
                    stream.close()
                    upstream.events[token_a][11] = {**live, "event_id": 11}
                    printed = await asyncio.wait_for(second.stderr.readline(), 5)
                    assert json.loads(printed) == {**live_notice, "event_id": 11}
                    await until(lambda: not upstream.events[token_a])
                assert await second.stderr.read() == b""

    asyncio.run(scenario())


def test_blocked_notice_output_does_not_block_ack_or_local_queries():
    from teamup_client.ipc import call

    async def scenario(write_fd, read_fd):
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            async with gateway() as (upstream, server_url):
                config, socket = write_config(Path(name), server_url)
                member_key = BINDINGS[0]["binding_key"]
                member_token = BINDINGS[0]["member_token"]
                upstream.registered[member_token] = {
                    "member_id": MEMBERS[0],
                    "role_description": "dev",
                }
                notice = {
                    "event_id": 10,
                    "room_id": ROOM,
                    "message_id": 11,
                    "sender_member_id": MEMBERS[1],
                }
                upstream.events[member_token] = {
                    10: {"event_id": 10, "kind": "mention", "message": notice}
                }
                async with daemon(config, socket, stderr=write_fd):
                    try:
                        async with asyncio.timeout(3):
                            await until(lambda: not upstream.events[member_token])
                            assert await call(
                                socket, member_key, "notifications", {}
                            ) == {"notifications": [notice]}
                    finally:
                        os.set_blocking(read_fd, False)
                        with suppress(BlockingIOError):
                            while os.read(read_fd, 8192):
                                pass

    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        for chunk in (b"x" * 512, b"x"):
            with suppress(BlockingIOError):
                while os.write(write_fd, chunk):
                    pass
        os.set_blocking(write_fd, True)
        asyncio.run(scenario(write_fd, read_fd))
    finally:
        os.close(write_fd)
        os.close(read_fd)


def mention_event(event_id, message_id, text="Please review the schema"):
    return {
        "event_id": event_id,
        "kind": "mention",
        "message": {
            "message_id": message_id,
            "room_id": ROOM,
            "sender_member_id": MEMBERS[1],
            "client_message_id": f"m{message_id}",
            "text": text,
            "mention_member_id": MEMBERS[0],
        },
    }


def test_mention_raises_one_desktop_notice_and_tracks_delivery():
    from teamup_client.ipc import call

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            directory = Path(name)
            shown = directory / "shown.jsonl"
            async with gateway() as (upstream, server_url):
                config, socket = write_config(
                    directory, server_url, notify_command=recorder(shown)
                )
                token = BINDINGS[0]["member_token"]
                key = BINDINGS[0]["binding_key"]
                upstream.registered = {
                    token: {"member_id": MEMBERS[0], "role_description": "Backend"},
                    BINDINGS[1]["member_token"]: {
                        "member_id": MEMBERS[1],
                        "role_description": "Codex reviewer",
                    },
                }
                async with daemon(config, socket):
                    # Joining caches the room name used to label the notice.
                    await call(socket, key, "join_room", {"room_id": ROOM})
                    assert await call(socket, key, "mention_status", {}) == {
                        "unread_mentions": 0
                    }
                    upstream.events[token] = {
                        5: mention_event(
                            5, 4, "Please\n review\x1b[31m the schema " * 9
                        )
                    }
                    # The first ack fails, so the Server redelivers the mention.
                    upstream.ack_failures[5] = 1
                    sent = time.time()
                    upstream.streams[token].write(b"event: inbox_changed\ndata: {}\n\n")
                    await upstream.streams[token].drain()
                    await until(lambda: recorded(shown))
                    await until(lambda: not upstream.events[token])
                    [(when, title, subtitle, body)] = recorded(shown)
                    assert when - sent < 5
                    assert (title, subtitle) == ("Codex reviewer mentioned you", "team")
                    assert body.startswith("Please review[31m the schema Please")
                    assert len(body) == 140 and body.endswith("…")
                    assert "\n" not in body and "\x1b" not in body
                    assert await call(socket, key, "mention_status", {}) == {
                        "unread_mentions": 1
                    }
                    # Another binding on the same Client sees nothing.
                    other = BINDINGS[1]["binding_key"]
                    assert await call(socket, other, "mention_status", {}) == {
                        "unread_mentions": 0
                    }
                    notices = await call(socket, key, "notifications", {})
                    assert [item["event_id"] for item in notices["notifications"]] == [
                        5
                    ]
                    assert "text" not in notices["notifications"][0]
                    assert await call(socket, key, "mention_status", {}) == {
                        "unread_mentions": 0
                    }

                # Redelivery after a restart neither repeats the notice nor reopens it.
                upstream.events[token] = {5: mention_event(5, 4)}
                async with daemon(config, socket):
                    await until(lambda: not upstream.events[token])
                    await asyncio.sleep(0.5)
                    assert len(recorded(shown)) == 1
                    assert await call(socket, key, "mention_status", {}) == {
                        "unread_mentions": 0
                    }

    asyncio.run(scenario())


def test_offline_mentions_notify_once_in_order_and_failures_stay_unread():
    from teamup_client.ipc import call

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            directory = Path(name)
            shown = directory / "shown.jsonl"
            async with gateway() as (upstream, server_url):
                config, socket = write_config(
                    directory, server_url, notify_command=recorder(shown)
                )
                token = BINDINGS[0]["member_token"]
                key = BINDINGS[0]["binding_key"]
                upstream.registered = {
                    token: {"member_id": MEMBERS[0], "role_description": "Backend"},
                    # The sender's role names the failing notice for the recorder.
                    BINDINGS[1]["member_token"]: {
                        "member_id": MEMBERS[1],
                        "role_description": "fail",
                    },
                }
                # Mentions that arrived while this Client was offline.
                upstream.events[token] = {
                    3: mention_event(3, 2, "first"),
                    4: mention_event(4, 3, "second"),
                }
                async with daemon(config, socket):
                    await until(lambda: len(recorded(shown)) == 2)
                    await until(lambda: not upstream.events[token])
                    titles = [entry[1:] for entry in recorded(shown)]
                    room = f"Room {ROOM[:8]}"
                    assert titles == [
                        ["fail mentioned you", room, "first"],
                        ["fail mentioned you", room, "second"],
                    ]
                    # A failed desktop notice is still pending for the agent.
                    assert await call(socket, key, "mention_status", {}) == {
                        "unread_mentions": 2
                    }
                    page = await call(socket, key, "notifications", {"limit": 1})
                    assert [item["event_id"] for item in page["notifications"]] == [3]
                    assert await call(socket, key, "mention_status", {}) == {
                        "unread_mentions": 1
                    }
                async with daemon(config, socket):
                    await asyncio.sleep(0.5)
                    assert len(recorded(shown)) == 2

    asyncio.run(scenario())


def test_notice_store_upgrades_and_never_repeats_an_unsettled_attempt():
    import sqlite3

    from teamup_client._notices import NoticeStore

    db = sqlite3.connect(":memory:")
    db.execute("""
        CREATE TABLE notices (
            binding_scope TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            room_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            sender_member_id TEXT NOT NULL,
            PRIMARY KEY (binding_scope, event_id)
        ) WITHOUT ROWID
    """)
    db.execute("INSERT INTO notices VALUES ('s', 1, ?, 1, ?)", (ROOM, MEMBERS[1]))
    db.commit()
    store = NoticeStore(db)
    notice = {"room_id": ROOM, "message_id": 2, "sender_member_id": MEMBERS[1]}
    # Pre-upgrade notices count as unread but are not shown again.
    assert store.state("s", 1) == {
        "state": "shown",
        "attempts": 0,
        "harness_state": "skipped",
    }
    assert store.next_pending("s") is None
    assert store.record("s", {**notice, "event_id": 2}, "hi")
    assert not store.record("s", {**notice, "event_id": 2}, "hi")
    assert store.next_pending("s")["event_id"] == 2
    store.begin_attempt("s", 2)
    # The Client stopped before the notifier reported back.
    store.abandon_unsettled("s")
    assert store.state("s", 2) == {
        "state": "failed",
        "attempts": 1,
        "harness_state": None,
    }
    assert store.next_pending("s") is None
    assert store.unread("s") == 2
    store.mark_delivered("s", [1, 2])
    store.settle("s", 2, shown=True)
    assert store.state("s", 2)["state"] == "delivered"
    assert store.unread("s") == 0


def test_owner_methods_and_attach_reject_agent_credentials():
    from teamup_client._config import owner_key, read_config
    from teamup_client.ipc import ClientError, call

    install = {"binding_key": "G" * 42 + "A", "harness_instance_id": "claude-code@mac"}
    session = {"id": "session-1", "kind": "claude-code"}

    async def expect(code, *args, **kwargs):
        with pytest.raises(ClientError) as failure:
            await call(*args, **kwargs)
        assert failure.value.code == code

    async def scenario():
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            directory = Path(name)
            async with gateway() as (_upstream, server_url):
                config, socket = write_config(
                    directory, server_url, bindings=[], installs=[install]
                )
                owner = owner_key(read_config(config))
                key = install["binding_key"]
                async with daemon(config, socket):
                    # An install key names a harness, not a session.
                    await expect("invalid_request", socket, key, "find_members", {})
                    # Agents cannot reach owner operations with their own secret.
                    await expect(
                        "invalid_request", socket, key, "sessions", {}, session
                    )
                    await expect("unauthorized", socket, "H" * 42 + "A", "sessions", {})
                    assert await call(socket, owner, "sessions", {}) == {"sessions": []}
                    # The first call parks the session for the owner.
                    await expect(
                        "session_pending", socket, key, "find_members", {}, session
                    )
                    report = (await call(socket, owner, "sessions", {}))["sessions"]
                    assert [
                        (item["session_key"], item["state"]) for item in report
                    ] == [("session-1", "pending")]
                    await expect(
                        "not_found",
                        socket,
                        owner,
                        "set_wake",
                        {
                            "harness_instance_id": "claude-code@mac",
                            "session_key": "session-1",
                            "rooms": [ROOM],
                            "senders": [],
                        },
                    )
                    await call(
                        socket,
                        owner,
                        "decide_session",
                        {
                            "harness_instance_id": "claude-code@mac",
                            "session_key": "session-1",
                            "state": "rejected",
                        },
                    )
                    await expect(
                        "unauthorized", socket, key, "find_members", {}, session
                    )

                    # attach needs a Claude Code session and a socket this user owns.
                    for bad_session, address in (
                        ({"id": "session-1", "kind": "codex"}, str(socket)),
                        (session, str(config)),
                        (session, "relative.sock"),
                    ):
                        await expect(
                            "invalid_request",
                            socket,
                            key,
                            "attach",
                            {"address": address, "token": "t"},
                            bad_session,
                        )

    asyncio.run(scenario())


def test_unsettled_submission_is_never_resent():
    import sqlite3

    from teamup_client._notices import NoticeStore

    store = NoticeStore(sqlite3.connect(":memory:"))
    notice = {"room_id": ROOM, "message_id": 2, "sender_member_id": MEMBERS[1]}
    for event_id in (1, 2, 3):
        store.record("s", {**notice, "event_id": event_id})
    store.set_harness_state("s", 1, "submitting")
    store.set_harness_state("s", 2, "waiting")
    store.abandon_submitting("s")
    assert store.state("s", 1)["harness_state"] == "unknown"
    assert [item["event_id"] for item in store.undecided("s")] == [2, 3]
