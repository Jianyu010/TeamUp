import asyncio
import json
import time

import pytest
from _stack import ROOT, clean_env, command, deploy, run_process, tool, wait_for


def notifications_cli(dev):
    """``teamup-client notifications``: the public CLI over the same IPC socket."""
    argv, env = command("client")
    out = run_process(
        [*argv, "notifications"],
        capture_output=True,
        text=True,
        env=clean_env()
        | env
        | {
            "TEAMUP_SOCKET": str(dev.socket_path),
            "TEAMUP_BINDING_KEY": dev.binding_key,
        },
        cwd=ROOT,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["notifications"]


async def run(alice, bob):
    async with alice.agent() as a, bob.agent() as b:
        a_member = await tool(
            a,
            "register",
            name="Backend",
            description="Owns backend API behavior and service handoffs.",
        )
        b_member = await tool(
            b,
            "register",
            name="Frontend",
            description="Owns interface behavior and coordinates API integration.",
        )
        room = (
            await tool(
                a, "create_room", name="API compatibility", rules="Share test data"
            )
        )["room_id"]
        board = await tool(a, "get_board", room_id=room)
        await tool(a, "ack_board", room_id=room, version=board["version"])
        await tool(b, "join_room", room_id=room)
        board = await tool(b, "get_board", room_id=room)
        await tool(b, "ack_board", room_id=room, version=board["version"])

        # A plain room message creates no mention notice and no desktop alert.
        plain = await tool(
            a,
            "send_message",
            room_id=room,
            client_message_id="plain",
            text="FYI: the schema changed.",
        )
        await asyncio.sleep(1)
        assert alice.desktop_notices() == [] and bob.desktop_notices() == []

        ping = {
            "room_id": room,
            "client_message_id": "live-ping",
            "text": "Can you review the response shape?",
            "mention_member_id": b_member["member_id"],
        }
        sent = time.time()
        live = await tool(a, "send_message", **ping)
        # Resending the same request returns the original receipt; changing it conflicts.
        assert await tool(a, "send_message", **ping) == live
        await tool(
            a, "send_message", error="conflict", **(ping | {"text": "different"})
        )
        notice = await asyncio.to_thread(bob.wait_for_notice, live["message_id"])
        assert notice["sender_member_id"] == a_member["member_id"]
        assert "text" not in notice  # notices carry no message body
        [shown] = await asyncio.to_thread(
            wait_for, bob.desktop_notices, bob.proc, 15, "desktop notice"
        )
        assert shown[0] - sent < 5
        assert shown[1:] == [
            "Backend mentioned you",
            "API compatibility",
            "Can you review the response shape?",
        ]
        assert alice.desktop_notices() == []
        assert plain["message_id"] not in {n["message_id"] for n in bob.notices()}
        # Sender never gets her own mention.
        assert all(n["message_id"] != live["message_id"] for n in alice.notices())

        # The unread hint rides on tool results until notifications are read.
        response = await b.call_tool("teamup_get_board", {"room_id": room})
        assert "1 unread mention" in response.content[-1].text
        assert (await tool(b, "notifications"))["notifications"] == [notice]
        response = await b.call_tool("teamup_get_board", {"room_id": room})
        assert len(response.content) == 1
        texts = [
            m["text"]
            for m in (await tool(b, "read_messages", room_id=room))["messages"]
        ]
        assert texts == [
            "FYI: the schema changed.",
            "Can you review the response shape?",
        ]

    # Bob goes offline; the mention waits on the Server and arrives exactly once.
    bob.stop()
    async with alice.agent() as a:
        offline = await tool(
            a,
            "send_message",
            **(
                ping
                | {
                    "client_message_id": "offline-ping",
                    "text": "Check this when you get back.",
                }
            ),
        )
    bob.start()
    backfill = await asyncio.to_thread(bob.wait_for_notice, offline["message_id"])
    await asyncio.to_thread(
        wait_for,
        lambda: len(bob.desktop_notices()) == 2,
        bob.proc,
        15,
        "2 desktop notices",
    )
    assert bob.desktop_notices()[1][3] == "Check this when you get back."
    assert [n["message_id"] for n in notifications_cli(bob)] == [
        live["message_id"],
        offline["message_id"],
    ]
    assert backfill["message_id"] == offline["message_id"]

    # Restarting again must not replay anything.
    bob.stop()
    bob.start()
    await asyncio.sleep(2)
    assert len(bob.desktop_notices()) == 2
    assert "message_id" not in bob.log.read_text(errors="replace")


@pytest.mark.scenario(
    "NS-07",
    "Room message, @mention live notice, offline backfill exactly once, idempotent resend",
)
def test_mention_delivery(ws):
    _, (alice, bob) = deploy(ws)
    asyncio.run(asyncio.wait_for(run(alice, bob), 90))
