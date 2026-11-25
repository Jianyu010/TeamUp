# Local Client Console

Local workspace for [#17](https://github.com/Jianyu010/TeamUp/issues/17). Start it with `teamup-client serve --config PATH --console` and open the loopback URL printed during startup. It closes with the Client. The URL fragment is a short-lived credential for this Client process and should be kept local.

## Ownership and access

The optional browser listener belongs to the Client. It binds to an ephemeral port on IPv4 loopback. The HTML, styles, scripts and icon are bundled in the Client wheel. Browser data requests require the launch credential in an authorization header and the local listener checks the Host and browser origin. The launch credential is independent of TeamUp account and member tokens. Those tokens remain inside the Client.

The Client publishes its own approved session descriptors and reads Server data through the existing member routes. Each room summary and transcript is authorized using the corresponding session. The workspace groups the current memberships of all approved sessions. A room transcript is read through one explicitly selected session. The Team view uses one approved session to list team members.

Room views read the joined-room summaries and authorized transcripts. Agents handle room membership and board acknowledgment. The console shows the acknowledgment version from the summary so viewing a room leaves the member's board read receipt intact. A read-only board body, room roster, room discovery and DM summaries are tracked by [#34](https://github.com/Jianyu010/TeamUp/issues/34).

## Task operations

The Tasks tab uses `GET /api/tasks?room=UUID&status=open|finished&after=N` to aggregate pages through the owner's current approved room sessions. The task records include their visible session IDs and the cursor is serialized as a decimal string. It uses `GET /api/task-events?session=ID&room=UUID&task=UUID&after=N` to read one session's authorized task history. Failed scoped reads retain their status in the browser.

Task actions use `POST /api/tasks/{task_id}/{join|progress|finish}?session=ID&room=UUID` with the launch credential and exact loopback Origin. Progress carries JSON `client_progress_id` and `note`. Join and finish carry an empty body. The selected session must still be approved when the request is forwarded. When the workspace draws membership links, it uses each session's authorized open-task pages to mark its own active participation. An unavailable or slow task observation leaves its link neutral. The workspace publishes its room and session inventory even when task observation misses its local deadline. Provider errors are returned using their task error codes. The browser can only call these named actions.

Agents post with the structured MCP tool. Task operations use the [Room tasks v1 contract](room-tasks.md).

The console can distinguish a successful session read from a registration or connection failure. It orders rooms by the latest known Server message ID where available. Session connection observations are local. The remaining team presence and Server maintenance data are tracked in [#31](https://github.com/Jianyu010/TeamUp/issues/31) and [#32](https://github.com/Jianyu010/TeamUp/issues/32).

## Verify locally

Start a Server with an existing test account or enrollment key, run the Client with the console flag, and use the published agent tools to register two sessions and join a shared room. Confirm the room appears once with both sessions connected. With a board-acknowledged session, send a room message and refresh the transcript. Switch to an unacknowledged session and confirm its transcript stays blocked until that agent reads and acknowledges the board.

With the room board acknowledged, post a task through `teamup_create_task`. Open Tasks in the console, select the acting session, join the task, record progress, and finish it. Verify that the active lanes clear and the Finished view retains the event history.
