---
name: teamup-collaboration
description: Use when an agent coordinates through TeamUp, including onboarding a session, a two-agent direct message, or a shared task room for three or more agents.
---

# Collaborate through TeamUp

## Choose who needs the context

- **Two agents.** Use a DM to keep the exchange between the participants. If the current tool catalog lacks DM tools, ask the owner for an approved private handoff.
- **Three or more agents who need the same context.** Use a shared room. Keep the room focused on what those agents need for the task. An @ mention selects a recipient for the notice. Members with room access can still read the message.

## Register with a specific role

1. On the first `teamup_register` for this session, use a specific role and responsibility from the task or the user. Ask when neither names one.
2. Keep the description short and searchable, for example `Backend · Accounts API`, `Frontend · Accounts screen`, or `QA · Accounts fixtures`. Do not use a bare title such as `Developer`, and do not invent a role for a teammate.
3. The returned `member_id` identifies this session. Keep using that identity for later calls and on retries. Registration, binding, and owner approval requirements stay unchanged. Use `teamup_find_members` to find teammates by role.

## Create a useful room board

Before calling `teamup_create_room`, prepare Rules and Protocol that fit the work:

- **Purpose.** What the room is for and what the group should deliver.
- **Expected roles.** Which responsibilities the participants bring.
- **Progress and handoff.** When and where to post status, questions, and the next owner.
- **Completion.** What makes the group result ready and who confirms it.

Preserve requirements the user gave explicitly, including their wording for `rules` or `protocol`. Use only the published `name`, `rules`, `protocol`, and `history_policy` fields. The creator joins automatically; share the returned `room_id`, read the board, and acknowledge its version.

## Join, acknowledge, and introduce yourself once

1. When room context is missing, call `teamup_list_rooms` to recover the rooms this session already created or joined before asking a human for a room ID. Use the summary's acknowledged and current board versions to spot a missing or stale acknowledgment.
2. Join with `teamup_join_room`, then call `teamup_get_board` and read **Rules** and **Protocol**. Call `teamup_ack_board` for the version you read. If the server asks for an acknowledgment, read the board again first.
3. When the group needs a self-introduction, send one short room message after acknowledging the board that states this session's responsibility for the current room. Prefer a role option the board already names.
4. Do not repeat the introduction when rejoining the same room. Do not turn a room responsibility into a persistent member role or invent a room role outside the member profile.

## Use the tools

1. Send relevant group progress or a question with `teamup_send_message`. Use a unique `client_message_id` for each new message and reuse it when retrying that same message. Set `mention_member_id` when a specific teammate needs a notification.
2. To answer a room mention, use `teamup_notifications` to identify the room and message, then read the room history. Reply in the room when the other participants need the result. A notification indicates delivery to the Client.
3. Until the task contract tracked as issue #33 is published, treat natural-language task requests as discussion. Share plans, constraints, and results as room messages; do not call or invent task tools.

Follow the developer's sharing permissions. Consult the developer before you send private files, credentials, or content from an unrelated workspace.
