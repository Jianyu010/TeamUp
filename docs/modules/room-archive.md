# Room archive

The room creator can mark the room’s goal reached. A room also closes after 24 elapsed hours without a new committed chat, measured from its creation when empty. Joins, Board changes, tasks, delivery acknowledgments, and retries do not extend the deadline. The server runs the inactivity sweep at startup and every 15 seconds, with immediate successive batches while more rooms may be due. A new chat at or after the deadline is rejected even between sweeps.

## Ownership and storage

Collaboration owns the archive transition. In one SQLite write transaction, it records the UTC archive time and the reason (`goal_reached` or `inactive`), captures the roster at closing, and makes live operations ineligible. The roster preserves each member’s ID, display information, responsibility, and last Board acknowledgment. Historical join activity remains with the room. The snapshot is stable even if members later retire or change their profiles. Existing room IDs, messages, attachments, Board content, and task records stay in the canonical database. Upgrades use the newest verifiable chat timestamp or the recorded creation time. A legacy room whose inactivity start cannot be established gets 24 hours from migration.

A closed room leaves the active room list, public directory, and delivery eligibility. Active-only operations include joining, leaving, updating the roster or Board, creating or changing tasks, sending messages, and fetching chats for delivery. Pending room messages, mentions, and task reminders are no longer eligible for delivery or acknowledgment. The server can signal that the inbox changed without sending chat bodies. Archive writes and room mutations serialize at the Collaboration boundary. Chat bodies already handed to a recipient before closing may already be in transit.

## Member interface

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/rooms/{room_id}/archive` | Mark the goal reached using the current room-creator authority, without a request body. A successful response returns `room_id`, `name`, `archived_at`, and `archive_reason`. An authorized retry returns the same record, including when inactivity won the race. |
| `GET /v1/rooms/archive` | List archive records available to the bound member, with an optional exclusive room UUID `after` and `limit` from 1 to 100 (default 50). Returns `rooms` and nullable `next_after`. |

The current room-creator rule matches Board publication. An unauthorized credential yields `401 unauthorized`, a caller outside the room yields `404 not_found`, and a joined non-creator closing a room yields `403 forbidden`. Other active-only operations return `409 room_archived` to members and `404 not_found` to outsiders when appropriate. Archived room details, roster, Board view, tasks, history, and search continue through the existing member reads. The archived roster omits actionable membership IDs. Transcript and task reads keep the existing Board acknowledgment and history-scope requirements. Acknowledging an already acknowledged final Board version may succeed idempotently. Retired-session history and operator transcript grants continue to use their published authorization rules. The operator console shows archive status and the same closing roster under its existing room metadata grant.

The Client and MCP use these Gateway endpoints with an approved member binding. MCP publishes `teamup_archive_room` and `teamup_list_archived_rooms`. The Client console provides a creator action and read-only archive access. Older clients can continue to use existing room reads by ID, and the active-room list keeps its existing shape.

## Retention and recovery

The server retains both room and direct-message history indefinitely. The former `--room-retention-hours` and `--dm-retention-hours` startup flags are accepted for compatibility and log that expiration is disabled. Existing expiration watermarks still report gaps from earlier releases. Full SQLite snapshots include the archive via the existing [scheduled off-host backup](server-backup.md). Verify a fresh hp-laptop snapshot and restore the data in isolation after deploying the schema change. The backup recovery point is the latest successful snapshot.
