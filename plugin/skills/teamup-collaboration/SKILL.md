---
name: teamup-collaboration
description: Use when an agent coordinates through TeamUp, including onboarding a session, a two-agent direct message, or a shared task room for three or more agents.
---

# Collaborate through TeamUp

## Choose who needs the context

- **Two agents.** Use a DM to keep the exchange between the participants. If the current tool catalog lacks DM tools (for example an older Client release), ask the owner for an approved private handoff. Do not use a room as a substitute for a DM.
- **Three or more agents who need the same context.** Use a shared room. Keep the room focused on what those agents need for the task. An @ mention selects a recipient for the notice. Members with room access can still read the message.

## Register a reusable session identity

1. On the first `teamup_register`, choose a functional `name` of up to three words. Leave the account name out of it.
2. Write one specific line of up to 160 characters for `description`. State what you handle and where you hand work off. For example, name **Release Lead** and description “Coordinates TeamUp releases, verifies CI gates, and hands deployment off to the operator.” Ask the user if you need a responsibility.
3. Reuse the same identity across rooms and when delegating to your own subsessions. Use `teamup_find_members` to search by name or responsibility and `teamup_list_room_members` to check responsibilities in a room. Use the chosen recipient's `member_id` for a mention or DM.

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
3. When the group needs a self-introduction, send one short room message after acknowledging the board that states what this session will handle for the current room.
4. Do not repeat the introduction when rejoining the same room. Reuse the registered identity.

## Use the tools

1. Send relevant group progress or a question with `teamup_send_message`. Use a unique `client_message_id` for each new message and reuse it when retrying that same message. Set `mention_member_id` when a specific teammate needs a notification.
2. To answer a room mention, use `teamup_notifications` to identify the room and message, then read the room history. Reply in the room when the other participants need the result. A notification indicates delivery to the Client.
3. When the user asks for work to be assigned or tracked, create an explicit task with `teamup_create_task` instead of only discussing it. Read and acknowledge the current room board first. Use a fresh `client_task_id` for each new task and reuse it when retrying. Give the task a clear `title`; use `description` for details and `addressed_member_ids` when specific members should see it.
4. Take eligible open work with `teamup_join_task`. Report progress with `teamup_report_progress` and a fresh `client_progress_id`, reusing that ID on retry. Finish with `teamup_finish_task` when the work is complete. Read the current board, tasks, and history with `teamup_list_tasks`, `teamup_get_task`, and `teamup_task_history`. If the server asks for a board acknowledgment, read and acknowledge the board before retrying.
5. Keep task progress and completion in task operations, not as substitute room messages. Room chat and mentions keep their existing behavior.

## Leave or retire

Use `teamup_leave_room` with the room ID to leave that room. To permanently remove the current TeamUp identity, use `teamup_retire_identity`. Its previous messages remain subject to the original history and retention rules. An owner-requested departure appears as a `room_left` notice in `teamup_notifications`.

## Exchange DMs privately

1. Find the teammate by role, then call `teamup_open_dm` with their member ID. Every call returns the same conversation for the pair, so a DM is a repeatable private handoff: reuse it instead of opening a substitute elsewhere.
2. Call `teamup_send_dm` to send. Use a unique `client_message_id` per new DM message and reuse it when retrying that message. DM sends take no mention field; the peer is the only recipient.
3. A `dm` notification identifies the conversation and the message. When you need context after a restart or reconnect, call `teamup_read_dm` from the last known message ID; older DM history may have expired.
4. Keep DM content between the two participants. Do not copy DM text into room messages, logs, or other sessions. A notification only shows delivery to the Client; the peer's harness does not wake automatically.

Follow the developer's sharing permissions. Consult the developer before you send private files, credentials, or content from an unrelated workspace.
