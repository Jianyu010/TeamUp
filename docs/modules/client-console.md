# Local Client Console

Local workspace for [#17](https://github.com/Jianyu010/TeamUp/issues/17). Start it with `teamup-client serve --config PATH --console` and open the loopback URL printed during startup. It closes with the Client. The URL fragment is a short-lived credential for this Client process and should be kept local.

## macOS desktop app

TeamUp.app opens one Electron window for the local profile ([#41](https://github.com/Jianyu010/TeamUp/issues/41)). The default profile is `~/.config/teamup/client.json`. The launcher reuses a running console from the same profile and socket or starts the installed Client through `teamup-client serve --config PATH --console`. The Client can continue receiving events and sending notifications after closing TeamUp.app. If the configured socket belongs to a Client started outside the launcher, TeamUp shows a startup error so the operator can choose how to manage that process.

The desktop app ships the Client's console assets and serves them inside the sandboxed window at `teamup://console/`. Its main process gets the loopback address and launch credential from the launcher over a private pipe. It holds that credential and forwards the console's named workspace, room, team, and task requests to the Client listener. The same Client session checks and Server authorization apply to both desktop and browser requests. See the [desktop build and launch instructions](../../client/desktop/README.md).

## Ownership and access

The optional browser listener belongs to the Client. It binds to an ephemeral port on IPv4 loopback. The HTML, styles, scripts and icon are bundled in the Client wheel. Browser data requests require the launch credential in an authorization header and the local listener checks the Host and browser origin. The launch credential is independent of TeamUp account and member tokens. Those tokens remain inside the Client.

The Client publishes its own approved session descriptors and reads Server data through the existing member routes. Each room summary and transcript is authorized using the corresponding session. The workspace groups the current memberships of all approved sessions. A room transcript is read through one explicitly selected session. The initial page contains the latest authorized messages. Older pages use the exclusive `before` cursor, and live catch-up uses the exclusive `after` cursor from the last displayed message. Both cursors use the published room history API and preserve the session's current access rules. The Team view uses one approved session to list team members.

Room views read the joined-room summaries and authorized transcripts. Agents handle room membership and board acknowledgment. The console shows the acknowledgment version from the summary so viewing a room leaves the member's board read receipt intact. A read-only board body, room discovery and DM summaries are tracked by [#34](https://github.com/Jianyu010/TeamUp/issues/34).

### Chats and Team read contexts

Chats keeps one joined, approved room session for the duration of the view. Entry prefers an acknowledged membership where available. If that session loses access, Chats discards its displayed pages before opening a replacement scope. Team tracks an approved registered session for its own directory reads. Tasks has its own **Acting as** selection, with approved joined sessions distinguished by identity. Changing it leaves the Chats and Team read contexts and the current transcript page intact.

The room transcript contains the messages committed by the Server within the reader's current [authorized history](conversation-history.md), including approved reads of exchanges between other members. The console maintains ascending Server message IDs and uses Server timestamps for display. Room pages expose the Server's optional `expired_through_message_id` as a decimal string and `history_truncated` as a boolean. The expiration watermark reports a cursor gap. Authorized older rows can survive below it. Layered pages are revalidated through the same session when history may have changed. The console explains membership and expiration boundaries with the policy and metadata available for the read.

A message's existing `mentioned` field gives the display name for its first Server-confirmed recipient or a short member identifier when directory resolution fails. Duplicate role labels are qualified by member identifier. A null value represents a broadcast. Messages with more than one confirmed recipient also include `mentioned_recipients` in Server target order. The renderer displays up to two recipient names and makes the remaining names accessible through the collapsed count. Message rows retain their Server `sender_member_id` and list structured `recipient_member_ids` for identity matching in room details.

### Members and Presence

`GET /api/room-members?session=ID&room=UUID` uses the room's stable read session. The Client reads the Server member roster and room detail with that member's authorization. Success includes `state: "ready"`, `current` (the room board version), `history_policy`, and `members`. Each roster entry has `id`, `role`, `acknowledged`, and `tasks`. Where the same session can read the room's active tasks, `tasks` lists the member's visible tasks with `id`, `title` and `teammates` containing member IDs and roles. An empty array represents a complete visible list with no tasks. A null task value represents an unavailable or incomplete observation. Recent communication comes from the latest matching row in the authorized transcript loaded in that view. A failed roster read omits room details and returns an empty member list.

Room Members and Team both project Presence through their own read session. Each member entry has a `presence` object with `state`, `observed_at`, and `age_ms`. A response can include `presence_as_of` for the Server snapshot timestamp. The Client displays observation age and expires Online using the Server's monotonic `age_ms` plus the elapsed time since sending the request. A missing observation, out-of-scope presence, or unavailable provider is shown as Unknown. Both views use green for Online, orange for Unknown, and red for Offline. They qualify duplicate role labels with the member identifier. Room membership, transcript access, and task authorization follow their respective owner APIs.

## Task operations

The Tasks tab uses `GET /api/tasks?room=UUID&status=open|finished&after=N` to aggregate pages through the owner's current approved room sessions. The task records include their visible session IDs and the cursor is serialized as a decimal string. It uses `GET /api/task-events?session=ID&room=UUID&task=UUID&after=N` to read one session's authorized task history. Failed scoped reads retain their status in the browser.

Task actions use `POST /api/tasks/{task_id}/{join|progress|finish}?session=ID&room=UUID` with the launch credential and exact loopback Origin. Progress carries JSON `client_progress_id` and `note`. Join and finish carry an empty body. The Client checks that the selected session is approved before dispatching the request. Locally rejecting a session prevents later dispatches and hides an in-flight result. The Server remains authoritative for an operation it has already received. When the workspace draws membership links, it uses each session's authorized open-task pages to mark its own active participation. An unavailable, slow, or incomplete task observation leaves its link neutral. The console bounds the number of open-task pages it reads for each activity link. The workspace publishes its room and session inventory even when task observation misses its local deadline. Provider errors are returned using their task error codes. The browser can only call these named actions.

Agents post with the structured MCP tool. Task operations use the [Room tasks v1 contract](room-tasks.md).

The console can distinguish a successful session read from a registration or connection failure. It orders rooms by the latest known Server message ID where available. Session connection observations are local. The remaining team presence and Server maintenance data are tracked in [#31](https://github.com/Jianyu010/TeamUp/issues/31) and [#32](https://github.com/Jianyu010/TeamUp/issues/32).

## Verify locally

Start a Server with an existing test account or enrollment key, run the Client with the console flag, and use the published agent tools to register two sessions and join a shared room. Confirm the room appears once with both sessions connected. With a board-acknowledged session, send a room message and refresh the transcript. Switch to an unacknowledged session and confirm its transcript stays blocked until that agent reads and acknowledges the board.

With the room board acknowledged, post a task through `teamup_create_task`. Open Tasks in the console, select the acting session, join the task, record progress, and finish it. Verify that the active lanes clear and the Finished view retains the event history.
