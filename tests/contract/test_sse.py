"""The SSE stream on GET /v1/events, captured from a real Server process."""

import json
import os
import socket
import subprocess
import sys
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from hashlib import sha256
from queue import Queue
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
from conftest import ROOT


def credential(label):
    return urlsafe_b64encode(sha256(label.encode()).digest()).decode().rstrip("=")


def auth(label):
    return {"Authorization": f"Bearer {credential(label)}"}


@contextmanager
def live_server(db_path):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "server/src"),
        "TEAMUP_ENROLLMENT_KEYS": json.dumps({"alice": credential("enroll-alice")}),
    }
    command = [sys.executable, "-m", "teamup_server.main", "--db", str(db_path)]
    with (
        subprocess.Popen(
            [*command, "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        ) as process,
        httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client,
    ):
        try:
            deadline = monotonic() + 10
            while monotonic() < deadline and process.poll() is None:
                try:
                    client.get("/v1/members/me")
                except httpx.ConnectError:
                    sleep(0.02)
                    continue
                break
            else:
                pytest.fail("Server did not start: " + str(process.stderr.read()))
            yield client, process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


def frame(lines):
    """Read one SSE frame (the lines up to the blank line) from the queue."""
    collected = []
    while True:
        line = lines.get(timeout=5)
        assert not isinstance(line, Exception), line
        assert line is not None, "stream closed early"
        if line == "":
            return collected
        collected.append(line)


def test_sse_frames_match_golden(tmp_path, golden):
    with live_server(tmp_path / "contract.sqlite3") as (client, process):
        for label in ("sender", "target"):
            response = client.post(
                "/v1/members",
                headers={"Authorization": f"Bearer {credential('enroll-alice')}"},
                json={
                    "harness_instance_id": "contract",
                    "session_key": label,
                    "role_description": label,
                    "member_token": credential(label),
                },
            )
            assert response.status_code == 200
        created = client.post(
            "/v1/rooms", headers=auth("sender"), json={"name": "Contract"}
        )
        room = f"/v1/rooms/{created.json()['room_id']}"
        for label in ("sender", "target"):
            if label == "target":
                joined = client.post(f"{room}/join", headers=auth(label))
                assert joined.status_code == 200
            assert client.get(f"{room}/board", headers=auth(label)).status_code == 200
            ack = client.post(
                f"{room}/board/ack", headers=auth(label), json={"version": 1}
            )
            assert ack.status_code == 200

        lines = Queue()
        meta = {}

        def read_stream():
            try:
                with client.stream(
                    "GET",
                    "/v1/events",
                    headers={**auth("target"), "Accept": "text/event-stream"},
                ) as response:
                    meta["status"] = response.status_code
                    meta["content_type"] = response.headers["content-type"]
                    for line in response.iter_lines():
                        lines.put(line)
            except httpx.HTTPError as exc:
                lines.put(exc)
            finally:
                lines.put(None)

        reader = Thread(target=read_stream, daemon=True)
        reader.start()
        frames = [frame(lines)]
        for key in ("one", "two"):
            sent = client.post(
                f"{room}/messages",
                headers=auth("sender"),
                json={"client_message_id": key, "text": key},
            )
            assert sent.status_code == 200
            frames.append(frame(lines))
        refused = client.get(
            "/v1/events",
            headers={"Authorization": "Bearer bad", "Accept": "text/event-stream"},
        )
        not_sse = client.get("/v1/events", headers=auth("target"))
        process.terminate()
        reader.join(timeout=5)
        assert not reader.is_alive(), "stream stayed open after shutdown"
    golden(
        "sse_events",
        {
            "content_type": meta["content_type"],
            "frames": frames,
            "status": meta["status"],
            "unauthorized": [refused.status_code, refused.json()["error"]["code"]],
            "without_accept": [not_sse.status_code, not_sse.json()["error"]["code"]],
        },
    )
