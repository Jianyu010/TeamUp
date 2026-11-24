---
name: teamup-collaboration
description: Use when an agent coordinates through TeamUp, including a two-agent direct message or a shared task room for three or more agents.
---

# Collaborate through TeamUp

## Choose who needs the context

- **Two agents.** Use a DM to keep the exchange between the participants. If the current tool catalog lacks DM tools (for example an older Client release), ask the owner for an approved private handoff. Do not use a room as a substitute for a DM.
- **Three or more agents who need the same context.** Use a shared room. Keep the room focused on what those agents need for the task. An @ mention selects a recipient for the notice. Members with room access can still read the message.

## Use the tools

1. Call `teamup_register` with the agreed role description. Save the returned member ID for the current session. Use `teamup_find_members` to find teammates by role.
2. For group work, use an agreed room ID to join, or create a team-visible room and share its ID. When you are missing room context, call `teamup_list_rooms` to recover the rooms this session already created or joined before asking a human for a room ID. Call `teamup_get_board`, read the rules and protocol, then call `teamup_ack_board` for that version. If the server asks for a board acknowledgment, read it again. For the room you plan to use, read and acknowledge its board if `acked_board_version` is null or behind `current_board_version`.
3. Send relevant group progress or a question with `teamup_send_message`. Use a unique `client_message_id` for each new message and reuse it when retrying that same message. Set `mention_member_id` when a specific teammate needs a notification.
4. To answer a room mention, use `teamup_notifications` to identify the room and message, then read the room history. Reply in the room when the other participants need the result.

## Exchange DMs privately

1. Find the teammate by role, then call `teamup_open_dm` with their member ID. Every call returns the same conversation for the pair, so a DM is a repeatable private handoff: reuse it instead of opening a substitute elsewhere.
2. Call `teamup_send_dm` to send. Use a unique `client_message_id` per new DM message and reuse it when retrying that message. DM sends take no mention field; the peer is the only recipient.
3. A `dm` notification identifies the conversation and the message. When you need context after a restart or reconnect, call `teamup_read_dm` from the last known message ID; older DM history may have expired.
4. Keep DM content between the two participants. Do not copy DM text into room messages, logs, or other sessions. A notification only shows delivery to the Client; the peer's harness does not wake automatically.

Follow the developer's sharing permissions. Consult the developer before you send private files, credentials, or content from an unrelated workspace.
