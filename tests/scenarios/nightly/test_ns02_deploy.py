import asyncio
import json
import stat

import pytest
from _stack import (
    ROOT,
    clean_env,
    command,
    deploy,
    make_certificate,
    run_process,
    tool,
)


def sessions(dev, bin_dir):
    """The Client's public owner status surface: ``teamup-client sessions``."""
    argv, env = command("client", bin_dir)
    out = run_process(
        [*argv, "sessions", "--config", str(dev.config_path)],
        capture_output=True,
        text=True,
        env=clean_env() | env,
        cwd=ROOT,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["sessions"]


async def wire_check(server, devs, bin_dir):
    """Both daemons accept IPC, reach the Server and enroll their bindings."""
    members = []
    for dev in devs:
        async with dev.agent() as agent:
            member = await tool(
                agent,
                "register",
                name=f"{dev.name} role",
                description="Verifies a Client can register and reload its member.",
            )
            assert member == await tool(
                agent,
                "register",
                name=f"{dev.name} role",
                description="Verifies a Client can register and reload its member.",
            )
            members.append(member)
    api = server.api()
    try:
        for dev, member in zip(devs, members, strict=True):
            me = api.call("GET", "/v1/members/me", dev.bindings[0]["member_token"])
            assert me["member_id"] == member["member_id"]
        found = api.call("GET", "/v1/members", devs[0].bindings[0]["member_token"])
        assert {m["member_id"] for m in found["members"]} >= {
            m["member_id"] for m in members
        }
    finally:
        api.close()


def check_deployment(ws, bin_dir, tls):
    server, devs = deploy(ws, bin_dir=bin_dir, tls=tls)
    assert server.proc.alive()
    for dev in devs:
        assert dev.proc.alive()
        # Quickstart permissions: private directory and config, private socket.
        assert stat.S_IMODE(dev.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(dev.config_path.stat().st_mode) == 0o600
        assert dev.socket_path.is_socket()
        assert not dev.socket_path.stat().st_mode & 0o077
        [entry] = sessions(dev, bin_dir)
        assert entry["state"] == "approved" and entry["static"] is True
        assert entry["session_key"] == dev.bindings[0]["session_key"]
    asyncio.run(asyncio.wait_for(wire_check(server, devs, bin_dir), 60))
    # Stopping a daemon removes its liveness; the Server keeps serving.
    devs[1].stop()
    api = server.api()
    try:
        api.call("GET", "/v1/members/me", devs[0].bindings[0]["member_token"])
    finally:
        api.close()


@pytest.mark.scenario(
    "NS-02", "Installed server and two clients start, wire up and report healthy"
)
def test_deploy_installed_http(ws, workspace_venv):
    check_deployment(ws, workspace_venv, None)


@pytest.mark.scenario(
    "NS-11", "Server over TLS with a self-signed CA and Client ca_file"
)
def test_deploy_tls(ws, workspace_venv):
    tls = make_certificate(ws.root)
    if tls is None:
        pytest.skip("openssl is not available")
    check_deployment(ws, workspace_venv, tls)
    server_log = next(p for p in ws.procs if p.name == "server").text()
    assert "Traceback" not in server_log


@pytest.mark.scenario(
    "NS-12", "Client refuses a TLS Server whose certificate it does not trust"
)
def test_client_rejects_untrusted_certificate(ws):
    tls = make_certificate(ws.root)
    if tls is None:
        pytest.skip("openssl is not available")
    server, devs = deploy(ws, names=("alice",), tls=tls, start=False)
    config = json.loads(devs[0].config_path.read_text())
    config.pop("ca_file")
    devs[0].config_path.write_text(json.dumps(config))
    server.start()
    devs[0].start()

    async def run():
        async with devs[0].agent() as agent:
            await tool(
                agent,
                "register",
                error="unavailable",
                name="x",
                description="Checks registration while the Server is unavailable.",
            )

    asyncio.run(asyncio.wait_for(run(), 60))
