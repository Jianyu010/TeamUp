# Room tasks and progress events

Proposal for [#33](https://github.com/Jianyu010/TeamUp/issues/33) and the input to [#30](https://github.com/Jianyu010/TeamUp/issues/30). Collaboration, Gateway, Client and MCP owners must confirm the public contract before API implementation.

## Task creation

An agent interprets a natural-language request and calls a structured create operation. TeamUp validates and records a title, description and optional addressed member IDs. Ordinary room chat and @ delivery keep their existing semantics.

Collaboration is the authoritative owner of tasks, participants and task events. Gateway decodes requests. Client and MCP expose the same operations and do not infer task state from chat. The console reads the same projection through each authorized selected session.

A task has a Server-generated UUID, room ID, creator member ID, short title, optional description, zero or more addressed member IDs, participant member IDs, a version, and Server timestamps. Persist an open or finished state. Record `started_at` at the first join, `last_progress_at` only on progress, and `finished_at` on completion. All times are UTC. Existing room and message records keep their current shape.

## Proposed operations

| Member operation | Arguments after room and task IDs | Result |
| --- | --- | --- |
| Create a task | `client_task_id`, title, optional description, optional addressed member IDs | Open task with no participants |
| List tasks | Open or finished filter, stable cursor, bounded limit | Authorized task page |
| Read a task | None | Authorized task and participant summary |
| Join a task | None | Open task with the caller participating |
| Record progress | `client_progress_id`, note | Task with new progress time |
| Finish a task | None | Finished task and completion time |
| Read task history | Stable event cursor, bounded limit | Authorized ordered event page |

Use additive room-scoped Gateway routes. Add matching member IPC operations and MCP tools once reviewed. Follow v1 limits for the title, notes, request IDs, pagination and room or member IDs. Reject unknown fields and duplicate addressed IDs. Check that every addressed member belongs to the room during creation.

## Visibility and task views

Authorize the credential, current room membership, addressed scope and current board version on every read or write. Room or task IDs inaccessible to the caller use the same `not_found` behavior as absent IDs. For a visible task, an unacknowledged board uses the existing `board_ack_required` behavior. Apply the same checks to task history and previously issued cursors.

- An unaddressed task is visible and available to eligible room members. An addressed task is visible to the creator and addressed members. Only eligible addressed members can join an addressed task.
- Multiple eligible members can join the same open task. The first join starts it. A participant or the creator can finish it. Participants can record progress while it stays open.
- **Available** shows open tasks with zero participants. In the client, the addressed section only shows tasks addressed to one of the current user's sessions. The creator can trace their own addressed task through the task detail or archive.
- **Taken** shows unfinished tasks in which at least one of the user's currently authorized sessions participates.
- **Ongoing** shows authorized unfinished tasks with at least one participant. A task can appear in both Taken and Ongoing.
- Finishing removes the task from all three active views. It remains readable to eligible members in a finished list and history.

TeamUp must identify members by stable IDs. Role descriptions are display context. UI projections and presence are neither task authorization nor proof of progress.

## Transactions and retry

The Collaboration owner resolves authorization, reads or writes task state, and allocates and saves any event in one consistent transaction. Create and progress use member-scoped retry keys. A retry with identical inputs returns the same task without emitting another event. Reuse with altered inputs is a conflict. Recheck authorization before returning any retry result.

The first join sets the start time. Each distinct eligible member may join once. Concurrent distinct joins may both succeed. An identical join retry creates no second participant or event. Terminal completion and task changes serialize. A successful finish retry creates no further event. A join or progress committed after finish is rejected.

Task events have stable, monotonically increasing IDs and contain the task ID, actor ID, kind, Server time and optional progress note. Preserve order and availability across restart. State is derived from the committed record. Do not infer completion from a model message.

## Stalled task tracer

Trace active tasks from the last committed progress, or from the initial start when there has been no progress. Presence is a separate observation. Additional joins do not reset task progress. A reminder policy still needs the threshold, repeat interval and scope approved.

A later Server-owned tracer should check that a task is still unfinished and still qualifies before committing a durable reminder. Use an idempotency key for the task, progress position and reminder window. Stop after progress or completion, check participant access again, and use existing local delivery and wake permissions. Delivery semantics and scheduling remain part of #30.

## Review cases

- A non-member, a member from another room, and a room member outside the addressed audience cannot discover or mutate the task or its history.
- Two eligible participants join concurrently. Both are represented once. They appear together in Ongoing. Their own sessions also see the task in Taken.
- A concurrent finish and progress yields the committed ordering. Completion disappears from the active projections while the finished record and event persist.
- Duplicate create, join, progress and finish requests survive restart without duplicating records, changing timestamps or revealing an inaccessible task.
- A member who becomes ineligible or whom the current board excludes cannot reuse a cursor, cached metadata or an earlier retry key for access.
- A tracer retry cannot double-notify, and a later progress event or completion invalidates an earlier reminder candidate.
