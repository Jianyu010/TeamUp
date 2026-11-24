import asyncio
import datetime
import ipaddress
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_account_pilot import login, password_command
from test_loop import (
    ROOT,
    Developer,
    available_port,
    process,
    tcp_is_ready,
    tool,
    wait_for,
)

CADDY = os.environ.get("TEAMUP_CADDY_BIN") or shutil.which("caddy")

pytestmark = pytest.mark.skipif(
    CADDY is None, reason="set TEAMUP_CADDY_BIN or put caddy on PATH to opt in"
)


def generate_authority(common_name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def generate_leaf(authority_key, authority_certificate):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(authority_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    return key, certificate


def write_key(path, key):
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path


def write_certificate(path, certificate):
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path


class HttpsDeveloper(Developer):
    def __init__(self, root, name, https_port):
        super().__init__(root, name, https_port)
        config = json.loads(self.config_path.read_text())
        config.pop("enrollment_key")
        config["server_url"] = f"https://localhost:{https_port}"
        config["ca_file"] = "teamup-ca.pem"
        self.config_path.write_text(json.dumps(config))


@contextmanager
def caddy_process(config_path, env, log):
    with log.open("ab") as output:
        child = subprocess.Popen(
            [CADDY, "run", "--config", str(config_path), "--adapter", "caddyfile"],
            env=os.environ | env,
            stdout=output,
            stderr=output,
        )
        try:
            yield child
        except BaseException:
            sys.stderr.write(log.read_text(errors="replace"))
            raise
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


def create_account(database, username, password):
    password_command(
        [
            sys.executable,
            "-m",
            "teamup_server.main",
            "accounts",
            "--db",
            str(database),
            "create",
            username,
        ],
        password,
        prompts=2,
        env={"PYTHONPATH": str(ROOT / "server/src")},
    )


def wait_for_https(origin, context, child):
    def verified():
        try:
            response = httpx.get(
                f"{origin}/v1/inbox", verify=context, trust_env=False, timeout=2
            )
        except httpx.HTTPError:
            return False
        assert response.status_code == 401, response.status_code
        return True

    wait_for(verified, child)


def tls_verification_failed(exc):
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        pending.append(current.__cause__)
        pending.append(current.__context__)
    return False


async def https_exchange(root):
    app_port = available_port()
    https_port = available_port()
    origin = f"https://localhost:{https_port}"

    pki = root / "pki"
    pki.mkdir()
    ca_key, ca_certificate = generate_authority("TeamUp HTTPS acceptance CA")
    leaf_key, leaf_certificate = generate_leaf(ca_key, ca_certificate)
    _, untrusted_certificate = generate_authority("TeamUp HTTPS untrusted CA")
    ca_path = write_certificate(pki / "teamup-ca.pem", ca_certificate)
    untrusted_path = write_certificate(pki / "untrusted-ca.pem", untrusted_certificate)
    certificate_path = write_certificate(pki / "localhost.pem", leaf_certificate)
    key_path = write_key(pki / "localhost.key", leaf_key)
    trusted = ssl.create_default_context(cafile=str(ca_path))
    untrusted = ssl.create_default_context(cafile=str(untrusted_path))

    database = root / "server.db"
    usernames = ["https-alice", "https-bob"]

    with (
        process(
            "teamup_server.main",
            "server/src",
            ["--db", database, "--port", app_port],
            root / "server.log",
        ) as server,
        caddy_process(
            ROOT / "deploy" / "Caddyfile",
            {
                "TEAMUP_HTTPS_HOST": "localhost",
                "TEAMUP_HTTPS_PORT": str(https_port),
                "TEAMUP_HTTPS_BIND": "127.0.0.1",
                "TEAMUP_TLS_CERT": str(certificate_path),
                "TEAMUP_TLS_KEY": str(key_path),
                "TEAMUP_UPSTREAM_PORT": str(app_port),
                "XDG_DATA_HOME": str(root / "caddy-home"),
                "XDG_CONFIG_HOME": str(root / "caddy-home"),
            },
            root / "caddy.log",
        ) as proxy,
    ):
        wait_for(lambda: tcp_is_ready(app_port), server)
        wait_for_https(origin, trusted, proxy)

        async with httpx.AsyncClient(
            base_url=origin, verify=trusted, trust_env=False
        ) as api:
            assert (await api.get("/")).status_code == 404
            assert (await api.get("/v1foo")).status_code == 404
            rejected = await api.get(
                "/v1/inbox",
                headers={"Authorization": "Bearer not-a-member-token"},
            )
            assert rejected.status_code == 401
            assert rejected.json()["error"]["code"] == "unauthorized"

        a = HttpsDeveloper(root, "alice", https_port)
        b = HttpsDeveloper(root, "bob", https_port)
        for developer in (a, b):
            shutil.copy(ca_path, developer.config_path.parent / "teamup-ca.pem")
        for developer, username in zip((a, b), usernames, strict=True):
            password = secrets.token_urlsafe(24)
            create_account(database, username, password)
            login(developer, username, password)
            assert "account_token" in json.loads(developer.config_path.read_text())

        async with httpx.AsyncClient(
            base_url=origin, verify=trusted, trust_env=False
        ) as api:
            denied = await api.post(
                "/v1/auth/login",
                json={"username": usernames[0], "password": "wrong password"},
            )
            assert denied.status_code == 401
            assert denied.json()["error"]["code"] == "unauthorized"

        async with httpx.AsyncClient(
            base_url=origin, verify=untrusted, trust_env=False
        ) as untrusted_client:
            with pytest.raises(httpx.ConnectError) as rejected:
                await untrusted_client.get("/v1/inbox")
            assert tls_verification_failed(rejected.value)

        with a.daemon():
            async with a.agent() as agent_a:
                with b.daemon() as bob_daemon:
                    async with b.agent() as agent_b:
                        alice = await tool(
                            agent_a, "register", role_description="HTTPS backend"
                        )
                        bob = await tool(
                            agent_b, "register", role_description="HTTPS frontend"
                        )
                        assert alice["member_id"] != bob["member_id"]
                        room = await tool(
                            agent_a, "create_room", name="HTTPS acceptance"
                        )
                        room_id = room["room_id"]
                        await tool(agent_b, "join_room", room_id=room_id)
                        for agent in (agent_a, agent_b):
                            board = await tool(agent, "get_board", room_id=room_id)
                            await tool(
                                agent,
                                "ack_board",
                                room_id=room_id,
                                version=board["version"],
                            )
                        # Let Bob's proxied SSE stream go live and idle so the
                        # mention is delivered by the live hint, not backfill.
                        await asyncio.sleep(1.5)
                        live = await tool(
                            agent_a,
                            "send_message",
                            room_id=room_id,
                            client_message_id="https-live-sse-hint",
                            text="Live hint over proxied SSE",
                            mention_member_id=bob["member_id"],
                        )
                    notice = await asyncio.to_thread(
                        b.wait_for_notice, live["message_id"], bob_daemon
                    )
                    assert notice["sender_member_id"] == alice["member_id"]
                    assert "text" not in notice
                offline = await tool(
                    agent_a,
                    "send_message",
                    room_id=room_id,
                    client_message_id="https-offline-backfill",
                    text="Backfill after offline reconnect",
                    mention_member_id=bob["member_id"],
                )
                with b.daemon() as bob_daemon:
                    backfill = await asyncio.to_thread(
                        b.wait_for_notice, offline["message_id"], bob_daemon
                    )
                    async with b.agent() as agent_b:
                        assert (await tool(agent_b, "notifications"))[
                            "notifications"
                        ] == [notice, backfill]
                        messages = (
                            await tool(agent_b, "read_messages", room_id=room_id)
                        )["messages"]
                        assert [message["text"] for message in messages] == [
                            "Live hint over proxied SSE",
                            "Backfill after offline reconnect",
                        ]


def test_caddy_https_proxy_login_sse_notice_and_reconnect():
    with tempfile.TemporaryDirectory(prefix="tu-https-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(120):
                await https_exchange(Path(directory))

        asyncio.run(run())
