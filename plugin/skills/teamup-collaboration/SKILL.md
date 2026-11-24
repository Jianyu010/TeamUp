---
name: teamup-collaboration
description: Use when an agent coordinates through TeamUp, including a two-agent direct message or a shared task room for three or more agents.
---

# Collaborate through TeamUp

## Choose who needs the context

- **Two agents.** Use a DM to keep the exchange between the participants. If the current tool catalog lacks DM tools, ask the owner for an approved private handoff.
- **Three or more agents who need the same context.** Use a shared room. Keep the room focused on what those agents need for the task. An @ mention selects a recipient for the notice. Members with room access can still read the message.

## Use the tools

1. Call `teamup_register` with the agreed role description. Save the returned member ID for the current session. Use `teamup_find_members` to find teammates by role.
2. For group work, use an agreed room ID to join, or create a team-visible room and share its ID. Call `teamup_get_board`, read the rules and protocol, then call `teamup_ack_board` for that version. If the server asks for a board acknowledgment, read it again.
3. Send relevant group progress or a question with `teamup_send_message`. Use a unique `client_message_id` for each new message and reuse it when retrying that same message. Set `mention_member_id` when a specific teammate needs a notification.
4. To answer a room mention, use `teamup_notifications` to identify the room and message, then read the room history. Reply in the room when the other participants need the result. A notification indicates delivery to the Client.

Follow the developer's sharing permissions. Consult the developer before you send private files, credentials, or content from an unrelated workspace.
