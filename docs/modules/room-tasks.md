# Room tasks v1

Contract for [#33](https://github.com/Jianyu010/TeamUp/issues/33) and [#30](https://github.com/Jianyu010/TeamUp/issues/30). Collaboration owns the tasks, participation and durable events. Gateway provides HTTP. Client and MCP carry the same operations for an approved session. The local console aggregates authorized reads for its owner and uses an explicitly selected session for mutations. Jianyu010, the verified repository administrator and assignee for both issues, approved proceeding with the task workflow on 2026-11-25. Provider coordination checked the Gateway authorization and Collaboration transaction boundaries. The hp-laptop Client/MCP review confirmed direct member-scoped pass-through and identified the browser write and legacy-response requirements incorporated below.

## Member operations

All paths below use the established member Bearer token. `{room_id}` and `{task_id}` are normal UUIDs.

| Method and path | Input | 200 result |
| --- | --- | --- |
| `POST /v1/rooms/{room_id}/tasks` | `client_task_id`, `title`, optional `description` and `addressed_member_ids` | Task |
| `GET /v1/rooms/{room_id}/tasks` | optional `status`, `after`, `limit` | `{"tasks":[Task], "next_after":number or null}` |
| `GET /v1/rooms/{room_id}/tasks/{task_id}` | none | Task |
| `POST /v1/rooms/{room_id}/tasks/{task_id}/join` | empty | Task |
| `POST /v1/rooms/{room_id}/tasks/{task_id}/progress` | `client_progress_id`, `note` | Task |
| `POST /v1/rooms/{room_id}/tasks/{task_id}/finish` | empty | Task |
| `GET /v1/rooms/{room_id}/tasks/{task_id}/events` | optional `after`, `limit` | `{"events":[TaskEvent], "next_after":number or null}` |

Task fields are `task_id`, `task_number`, `room_id`, `creator_member_id`, `title`, `description`, `addressed_member_ids`, `participant_member_ids`, `status` (`open` or `finished`), `version`, `created_at`, `updated_at`, `started_at`, `last_progress_at`, `finished_at`, and `stalled_since`. Optional description and unset timestamps are null. IDs in the two sets are sorted. `task_number`, `version` and event IDs are positive integers. Timestamps are Server-recorded RFC 3339 UTC. The initial version is 1 and each committed mutation increments it once.

TaskEvent fields are `event_id`, `task_id`, `actor_member_id`, `kind` (`created`, `joined`, `progress`, `finished`), `created_at`, and `note` (null except on progress). Task and event pages use ascending durable numbers with exclusive `after` (default 0) and `limit` (default 50, max 100). Filters are evaluated with current permissions on each page. `next_after` is the last returned number when another eligible row exists.

Titles follow the existing 200-character name limit. Description and progress note follow the existing 8,000-character text limit. Retry keys follow the 128-character message key limit. Omitting description is equivalent to null. Omitting addressed IDs is equivalent to an empty set. Cursors apply to the same room, active status filter and authorization scope that produced them. The addressed set is limited to 100 unique current room members. JSON rejects undeclared fields. Blank text is invalid.

## Permission and transaction rules

Authenticate, check current room membership, and check visibility of a targeted task before the board acknowledgment or operation precondition. Inaccessible and missing rooms or tasks use the same 404 `not_found`. An authorized task with a stale board returns 409 `board_ack_required`. A room task list checks the calling member's board before returning entries. Current room tasks and their events are collaboration records with their own visibility, independent of the chat history floor.

An unaddressed task is visible to room members. An addressed task is visible to the creator and addressed members. A member can join an open unaddressed task or an open task addressed to that member. Participants can record progress. The creator and participants can finish. Ineligible operations on an otherwise visible task return 404 `not_found`. Invalid addresses return 422 `invalid_request`.

Authorize and commit the record, participation and event in the same write transaction. Multiple distinct eligible participants may join concurrently. The first join sets `started_at`. Additional joins never change it. Only recorded progress updates `last_progress_at`. Finish is terminal and sets `finished_at`. A new join or progress after finish returns 409 `conflict`. If a write loses authorization first, authorization takes precedence over conflict.

Create and progress use separate member-scoped retry keys. Identical input returns the current projection for that task and emits no event. A changed payload or target for the same key returns 409 `conflict` after checking current authorization to the requested resource. An existing join or finish from an eligible member is an idempotent success even when the task has finished. Retry results never grant access from an earlier time.

## Agent and console views

MCP calls use the same field names plus the route IDs as arguments. IPC method names remove the `teamup_` prefix: `teamup_create_task`, `teamup_list_tasks`, `teamup_get_task`, `teamup_join_task`, `teamup_report_progress`, `teamup_finish_task`, `teamup_task_history`. Agents interpret natural requests and create explicit task records. Room chat and mentions keep their current behavior.

Available shows open tasks without participants, with addressed tasks shown for the owner's addressed local sessions. Taken shows open tasks involving at least one of the owner's current sessions. Ongoing shows authorized open tasks with participants. Finished is a separate history view. The local console can page scoped tasks through the Client. Agents post tasks through MCP. The console uses its local launch credential, explicit same-origin POST, and a selected authorized session for taking, reporting progress and finishing. Keys and message tokens stay inside the Client. A compatible Server is needed for task operations. An absent route and inaccessible task can share the existing not-found response. Clients show the operation as unavailable to the selected session without inferring an empty list or sending a chat message.

## Opt-in stalled-task tracing

The prior Client/MCP review called for independently paged reminders and unchanged mention delivery and wake rules. The provider owns reminder eligibility and the delivery claim.

The Server deployment sets both positive durations in seconds via `--task-stall-after` and `--task-reminder-interval`. Without the policy, tasks still work, `stalled_since` is null, and no reminders are issued. A task stalls at the configured threshold after its last recorded progress, or its first join if it has no progress. Subsequent joins and member presence do not reset the clock. Completion ends the stall.

The Collaboration owner can evaluate a bounded set of eligible recipients in a write transaction using a Server-owned periodic tick. Each open stalled task can have at most one pending reminder per current, board-acknowledged participant in its current activity period. A participant becomes eligible again after the reminder interval from the previous claim, or after progress and a subsequent stall. Activity periods are keyed by the committed first join or most recent progress event. Eligibility checks include current member/account state, room membership, task visibility, participation and the board version. New progress or completion invalidates pending reminders from the old activity period.

| Method and path | Input | 200 result |
| --- | --- | --- |
| `GET /v1/task-reminders` | optional `after`, `limit` | `{"reminders":[TaskReminder]}` |
| `POST /v1/task-reminders/{reminder_id}/claim` | empty | TaskReminder |

TaskReminder fields are `reminder_id`, `room_id`, `task_id`, `title`, and `stalled_since`. Eligible pending reminders are ordered by stable numeric ID. A claim is conditional and atomic. An inaccessible, invalidated or already-claimed reminder uses 404 `not_found`. A successful claim consumes the pending entry and is the only authorization to attempt one desktop notice. This is at-most-once notice delivery. A Client crash or lost response after a claim may suppress that attempt. The room still shows the stalled task.

Existing inbox payloads and wake privileges stay unchanged. The Server sends the existing inbox-change hint after committing reminders. The tracer wakes at a bounded interval, including when there are no Client connections. A compatible Client checks the reminder page on startup and on hints and uses its existing desktop notification configuration. An older Server can omit the new endpoint. Reminder delivery creates no room messages and starts no agent turns.

## Verification expectations

Cover addressed and unaddressed paths, board acknowledgment ordering, multiple participants, concurrent task changes, retry payload mismatches and restart persistence. Exercise the published HTTP boundary and MCP/Client paths. Check delivery and no duplicate claims across retry and restart, invalidation after progress or finish, and unchanged ordinary notifications. Verify the console updates its active task lanes and archive without mixing session scopes.
