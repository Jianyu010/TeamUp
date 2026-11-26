# Control Console room views

Rooms is a read-only view in the [Admin Console](admin-console.md). That contract covers navigation and account management.

**Stage 3 read endpoints are implemented within the scope set by the coordinator.** This stage builds on stage 2 (operator authentication, account- and room-level grants, and static hosting in commit `e495544`) and the #6 persistence baseline (commit `25fff63`, merged into this branch). It delivers `GET /v1/console/rooms/{room_id}` for roster details, `GET /v1/console/rooms/{room_id}/timeline` for activity metadata, and `GET /v1/console/rooms/{room_id}/transcript` for canonical room records with a same-transaction audit entry that omits message bodies. The completed #3 DM Server work (`codex/teamup-3-direct-messages`, covering DM tables and the inbox path) then merged into this branch. #3 brought the schema to **v4** with `dms`, nullable `messages.room_id` with an exactly-one-non-null constraint, retry indexes scoped by conversation type, and the `dm` inbox kind. The console additions brought it to **v5**. A forward merge then brought in the tested #6 room and DM retention runtime (`codex/teamup-6-retention` at `e86aa97`). It adds the two retention watermark columns in place and brings the schema to **v6**. The migration table below includes the released console v5 as a starting point. Migrations support fresh databases, v1, v2, both v3 drafts (#6 history and the released #5 authentication preview at `e495544`, distinguished by tables and columns during migration), #3 DM v4, and console v5. The console reads reuse #6's canonical message records and their owner. Member endpoint shapes stay compatible. The [DM contract](direct-messages.md) covers the merged #3 member DM endpoints. The [conversation history module contract](conversation-history.md) defines the #6 retention runtime. Production deployment is outside the scope of this stage. Track the work in [Control Console](https://github.com/Jianyu010/TeamUp/issues/5). See [Collaboration surfaces](../specs/collaboration-surfaces.md) for product behavior and the [conversation history specification](../specs/conversation-history.md) and [module contract](conversation-history.md) for history and auditing. This module contract covers operator authentication, read-only APIs, auditing, implementation, and tests. The [development protocol](../protocols/README.md) requires contract review before implementing new routes, schema changes, or permissions.

## Scope and ownership

| Role | Component | Responsibility |
| --- | --- | --- |
| Provider | Server Gateway | Publishes the read-only `/v1/console` routes and their errors. Serves the console's static assets as part of wiring the application. |
| State owner | Collaboration | Sole owner of permission checks, room state, message history, the activity projection, and audit records. |
| Consumer | Control Console Web UI | Read-only browser interface for human operators. Calls only the endpoints published in this contract. |

Affected consumers include the Console UI and account administration CLI subcommands delivered in stage 2, along with #6 member history reads. The console and history routes share the canonical records and owner, and member endpoint shapes stay compatible. Other existing consumers, member tokens, and enrollment keys continue to use the [Client–Server v1](client-server-v1.md) contract.

- The Rooms view provides no composer, join control, or write route. Opening it generates no chat activity.
- The console shows only team room records observed by TeamUp. DMs are private to their participants. The Timeline projection and migration backfill, roster `last_activity`, and transcript reads all use canonical room rows with a non-null `room_id`. Sending or acknowledging a DM generates no room activity. Even an operator with room-level `transcript_read` cannot retrieve DM text through Timeline or a transcript. The #3 Server merge added contract tests for this boundary.
- The harness status defaults to **Unknown**. There is no supported harness reporting channel in this stage, so `harness_state` is always `unknown`. The console must never infer it from SSE or connection state.
- A delivery receipt establishes that a Client received an event. It says nothing about whether an agent saw or handled it.

## Operator authentication

Model A applies. It reuses the account login pilot. The option to issue separate operator credentials was rejected.

**Model A reuses the account login pilot.** The pilot (commit `5f25dc6`, integrated into this worktree) provides accounts, Argon2id password hashing, eight-hour `account_token` credentials issued through `POST /v1/auth/login`, login rate limits, and session revocation on deactivation or password reset. The console adds two grants. Administrators configure them through the local CLI on the server, and Collaboration persists them.

| Grant | Scope | Allows |
| --- | --- | --- |
| `console_read` | Account | List rooms, read rosters, and view Timeline metadata. |
| `transcript_read` | Account × room | Read the room's Chats. Each successful page read is audited. |

After signing in, the browser sends the token as a Bearer credential to `/v1/console/*`. Accounts without `console_read` can sign in, but the console routes deny them access. The login body is `{"username":"…","password":"…","purpose":"console"}`. It adds the optional `purpose` field to `POST /v1/auth/login` from [Account login v1](account-login-v1.md), which defines the field and its only valid non-null value, `"console"`. A console session token authorizes only read-only console routes. It cannot enroll an agent via `POST /v1/members` or call ordinary member APIs. Enrollment-purpose login tokens, member tokens, and legacy enrollment keys cannot authorize console routes. The response remains `{"access_token":"…","expires_at":…}`, with expiration in Unix seconds.

### Configuring grants (local server CLI only)

The four subcommands under `teamup-server accounts --db PATH` use Collaboration's public entry points. Collaboration persists changes within its own transactions.

| Subcommand | Arguments | Behavior |
| --- | --- | --- |
| `grant-console` | `USERNAME` | Grants account-level `console_read`. Idempotent. |
| `revoke-console` | `USERNAME` | Revokes account-level `console_read` and leaves `transcript_read` in place. Idempotent. An unknown account returns not_found. |
| `grant-transcript` | `USERNAME ROOM_ID` | Grants the account `transcript_read` for the room. Idempotent. An unknown account or room returns not_found. An invalid room ID returns invalid_request. |
| `revoke-transcript` | `USERNAME ROOM_ID` | Revokes the grant. Idempotent. An unknown account or room returns not_found. An invalid room ID returns invalid_request. |

Collaboration is the sole owner of grants and stores them in `operator_grants` (`owner_id`, `scope` ∈ {`console_read`,`transcript_read`}, and `room_id`, which is NULL for `console_read` and references the room for `transcript_read`). Grant writes are available only through the CLI, which never prints credentials. Revocation and deactivation take effect on the next page. Each request rechecks session validity and the required grants in the transaction that uses them. A page request using an old cursor after a grant is revoked returns `forbidden`, or 401 if the session is invalid.

## Data shapes

| Name | JSON fields |
| --- | --- |
| Activity | `kind` is `message`, `member_joined`, `board_acknowledged`, or `delivery`. `at` is the timestamp in integer Unix seconds recorded by the Server, or null if the original row has no timestamp. `message_id` is a positive integer or null. Message and delivery activities reference the corresponding message. When #6 retention cleanup deletes a message, it clears the link to null and preserves the activity. |
| ConsoleRoom | `room_id` (UUID string), `name` (string), `member_count` (nonnegative integer), and `last_activity` (Activity or null). |
| ConsoleMember | `member_id` (UUID string), `role_description` (string), `board_state` (`awaiting` or `acknowledged` for the current Board version), `last_activity` (Activity or null), and `harness_state` (`unknown`). |
| ConsoleActivity | `activity_id` (positive integer), `room_id`, the Activity fields, and `actor_member_id` (for delivery, the member who acknowledged receipt). |
| ConsoleMessage | `message_id` (positive integer), `room_id`, `sender_member_id`, `sender_role_description` (the sender's registered role at read time), `text`, `mention_member_id` (UUID string or null), and `created_at` (integer Unix seconds recorded by the Server, or null for rows with no timestamp, which the UI displays as unknown). |
| TranscriptPage | `messages` is an array of ConsoleMessage objects in ascending `message_id` order. `has_more` is a boolean indicating whether any earlier messages exist at the current cursor. |

The current roster excludes retired members. Their historical messages keep their original `sender_member_id` and role description and add `sender_retired: true`. Chats displays `retired` next to the role. Transcript reads still require the same room grant and generate an audit entry.

UUIDs in paths use the standard hyphenated format. Text follows the length and encoding limits in [Client–Server v1](client-server-v1.md). The console payload returns `at` and `created_at` as integer Unix seconds for the consumer's `formatTime` to parse numerically. The #6 member endpoints keep `created_at` as an RFC 3339 string. The console projection converts canonical rows on read and stores no duplicate timestamp.

## Operations

Successful responses return HTTP 200. Pagination follows the published conventions. `after` starts at 0 and excludes IDs at or below that value. `limit` defaults to 50, with a maximum of 100. Results sort in ascending ID order. All console routes require a Bearer operator credential from an unexpired, unrevoked `purpose:"console"` session on an active account with `console_read`. Invalid or expired tokens, sessions of another purpose, member tokens, and enrollment keys all return 401 `unauthorized`. A valid credential without `console_read` returns 403 `forbidden`. A missing target room returns 404 `not_found` once the caller is authenticated and has `console_read`. Checks always run in this order: credential (401), account-level `console_read` (403), room existence (404), and room-level `transcript_read` (403, transcript only). Operators with `console_read` can already list all rooms and receive 404 for missing rooms on the transcript, roster, and timeline routes. Accounts without `console_read` get 403 whether or not the room exists.

| Method and path | Request | Success response | Stage |
| --- | --- | --- | --- |
| `GET /v1/console/rooms` | — | `{"rooms":[ConsoleRoom]}`, in stable `room_id` order. Covers team rooms only. `last_activity` is the room's latest `room_activity` projection, or null when there is no activity. | Stage 2/3. Stage 3 populates `last_activity` from the projection. |
| `GET /v1/console/rooms/{room_id}` | — | `{"room":ConsoleRoom,"members":[ConsoleMember],"transcript_allowed":boolean}`, with `members` in stable `member_id` order. `transcript_allowed` says whether the account currently has `transcript_read` for this room. The UI uses it to decide whether to request and display Chat data. | **Stage 3** |
| `GET /v1/console/rooms/{room_id}/timeline` | `after` and `limit` | `{"activities":[ConsoleActivity]}`, in ascending `activity_id` order. The UI pages with `after`. A page shorter than `limit` marks the end. Entries contain metadata only. `message_id` is returned, with null for an expired link and therefore no navigation target. The UI links to Chats only when `transcript_allowed` is true. | **Stage 3** |
| `GET /v1/console/rooms/{room_id}/transcript` | `before` (optional positive integer, exclusive upper bound) and `limit` | `TranscriptPage`. Omitting `before` returns the **latest page**, with messages in ascending `message_id` order and an accurate `has_more` flag. Use the oldest displayed `message_id` as `before` to fetch an earlier page. A positive `before` above SQLite's maximum ID is treated as an unbounded cursor, as when `before` is omitted. This preserves the legal maximum ID, matches the existing member-history boundary, and succeeds. The route reads canonical room records in ascending `message_id` order and writes one audit entry per successful response within the same transaction. Requires `transcript_read` for the target room. | **Stage 3** |

The authorization helper rechecks console session validity and required grants in the transaction that uses them. Every route checks `console_read`. The transcript route additionally checks `transcript_read` for the room. Grants are read from their source for each request and are never cached or projected. Authorization and persistence follow the stage 2 semantics.

Transcript pagination uses only `before`. The first request omits it to fetch the latest page directly, avoiding a scan from ID 0. Later requests use it to fetch earlier pages. This console contract does not add `before` to the published member history endpoint. The Timeline has no `has_more` field and uses a short page to detect the end.

### Caching

All successful (200) `/v1/console/*` responses send `Cache-Control: no-store`. The data can change with room activity and grants, so neither shared nor private caches may reuse it. Static hosting continues to use `no-cache` as described below. Error responses omit cache headers.

## Authorization, auditing, and errors

- Collaboration rechecks `transcript_read` on each transcript request. After revocation, a page request with an old cursor returns `forbidden`. When deactivation or password reset revokes a session, the next page returns 401.
- Collaboration stores the audit entry in the read transaction. It includes the operator `owner_id`, `room_id`, `action` (`transcript_read`), pagination parameters `before` and `limit`, and the Server timestamp. The audit captures the actual exclusive upper bound. Omitted and out-of-range unbounded `before` values both record null. It captures no message body, and the body cannot be inferred from the audit.
- Reading the room list, roster, or Timeline generates no audit entry.
- Errors use the existing `{"error":{"code":"...","message":"..."}}` format. The Gateway error table includes 403 `forbidden` for a valid credential without the required grant, used only by console routes. Existing semantics apply to 401 `unauthorized`, 404 `not_found`, 422 `invalid_request`, and 429 `rate_limited`.
- In the consumer UI, a 401 from any console route clears the in-memory session and returns to sign-in. A 403 displays a permission message in the affected area. The rooms area indicates missing `console_read`, and Chats indicates missing `transcript_read`. It does not retry automatically. A 404 shows that the room does not exist. A 429 or network error offers a retry. A transient error while fetching the next Timeline page or an earlier Chats page preserves the displayed rows and shows the error and retry control at the end of the list. The retry uses the same cursor as the failed request (the current `after` for Timeline or the failed `before` for Chats) and resumes pagination on success. A 403, a room switch, a manual refresh, or sign-out clears private content from both memory and the DOM.

## Static hosting (delivered in stage 2)

- The Gateway serves `GET /console/` (HTML), `GET /console/console.css`, `GET /console/console.js`, `GET /console/maintenance.js`, and `GET /console/accounts.js`. The response bytes match their respective files in the `server/src/teamup_server/console/` static bundle. Directory listing is disabled. Any other `/console/…` path returns 404 `not_found`, and unsupported methods return 405. `redirect_slashes` stays disabled, so `/console` without the trailing slash is unavailable.
- Response headers include `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-cache` to prevent stale assets before revisioning is available. The respective media types are `text/html; charset=utf-8`, `text/css; charset=utf-8`, and `text/javascript; charset=utf-8`.
- Static routes are public, and the page contains no private data. Console API responses under `/v1/console/*` require authentication. The Rooms view is read-only. Static hosting serves the browser interface.

## Console UI consumer contract (accepted in stage 2, transient pagination retries revised in #12 F3)

The Server Gateway provides routes and static hosting, implemented by the Server project. The consumer is the shipped Console Web UI bundle in `server/src/teamup_server/console/`. The following describes the call flows, the provider and consumer obligations, and compatibility.

1. **Static assets.** The same-origin paths are `/console/` (HTML), `/console/console.css`, and `/console/console.js`, one per static file. The HTML references only the two local resources using relative paths. The UI has no third-party scripts, styles, images, or fonts. The bundle contains no application logic, and the Gateway serves it at the three exact paths.
2. **Sign-in.** The browser sends `{"username","password","purpose":"console"}` to same-origin `POST /v1/auth/login` and receives `{"access_token","expires_at"}`.
3. **Token lifecycle.** The page holds the access token in a JavaScript variable. It never stores the token in cookies, localStorage, sessionStorage, or IndexedDB, and the token never appears in the URL or query string. The UI uses `Authorization: Bearer` for console GET requests. Its only POST is sign-in, above. Signing out, receiving a 401, or reaching expiration clears the token from memory and returns to sign-in. The UI holds no member tokens or enrollment keys and makes no enrollment calls.
4. **Request behavior.** When `transcript_allowed` is false, Chats shows that access is unavailable and makes no transcript requests. Timeline pages with `after`. Transcript requests omit `before` for the latest page and pass the oldest displayed message ID as `before` to load earlier messages. When `has_more` is false, the UI removes the control for fetching earlier messages. Time fields (`at`, `created_at`) display from numeric Unix seconds, with null rendered as unknown. A transient pagination failure (429, network error, or another retryable error) preserves the rows already loaded. Retrying sends the same cursor as the failed request and continues from that page. A permission denial, room switch, manual refresh, or sign-out clears private rows already loaded.
5. **Stale responses.** Abort in-flight requests on room switch, manual refresh, and sign-out. Before applying a response, check its room cursor so the UI never renders an old room's or signed-out session's data in a different room or a cleared view.
6. **Safe rendering.** Render all user text using `textContent`. Avoid `innerHTML` and other injection points. Keep all fetch requests same-origin.
7. **States and layout.** The read-only UI includes a room list (name, member count, latest activity), roster (role, Board acknowledgment, latest activity, and harness **Unknown**), and separate Timeline and Chats tabs. Both tabs have pagination and clear empty, loading, error, and permission states. The UI supports keyboard navigation and adapts its density to the viewport. The [mockup](../assets/teamup-control-console.excalidraw) guides content and layout.

Timeline displays an activity's associated `message_id` as `msg-N`, for example `msg-42`. It omits the reference when the ID is null. See the [conversation history contract](conversation-history.md#records-and-migration).

## Activity projection (Timeline data source)

- The `room_activity` table holds `activity_id` (globally increasing SQLite AUTOINCREMENT, so IDs are never reused after deletion), `room_id`, `kind` ∈ {`member_joined`,`board_acknowledged`,`message`,`delivery`}, `at` (RFC 3339 text or null), `actor_member_id`, and `message_id`. Message and delivery activities reference the corresponding message. Join and acknowledgment activities always have null `message_id`.
- **Retention compatibility (#5/#6).** The `message_id` foreign key uses `ON DELETE SET NULL`. A schema CHECK still requires null `message_id` for joins and Board acknowledgments. When #6 retention cleanup deletes expired messages and their delivery references in one cleanup transaction, the linked message and delivery activities lose their links. The activity rows, which omit the body, retain their `kind`, `at`, and actor. Links for retained messages and their existing visibility stay intact. Deleting the message itself removes access to its body. The activity rows remain available.
- Collaboration appends projection rows in **the same owning transaction** as four kinds of writes. For the creator's initial join or a member joining, it records `member_joined`, with the joining member as actor. For the first acknowledgment of a Board version, it records `board_acknowledged`, with the acknowledging member as actor and deduplication by version. When it accepts a message, it records `message`, with the sender as actor and `at` copied from the message's `created_at`. For a first delivery acknowledgment, it records `delivery`, with the recipient who acknowledged as actor and a reference to the message. Idempotent retries (join conflicts, duplicate acknowledgments, sending the same text with the same key, or repeated delivery acknowledgment) append no rows. Both projection and authorization use canonical membership, Board, and message records.
- The projection excludes DMs. A DM has NULL in `messages.room_id`, and DM sends and inbox acknowledgments append no `room_activity` rows. The message and delivery statements used during migration backfill likewise filter by `room_id IS NOT NULL`. Acknowledging a room message continues to record `delivery`.
- The projection is for display only. Authorization always checks canonical membership, Board, and message records. Message and delivery activities omit message bodies.
- The migration backfills historical activity that can be derived from canonical rows. It processes joins, then Board acknowledgments, then messages by `message_id`, then delivery acknowledgments by `event_id`. For an old row with no timestamp evidence, `at` is null. Message rows that have `created_at` preserve it. The backfilled rows contain only metadata and exclude all DMs.

## Audit storage

The `transcript_access_audit` table holds `owner_id`, `room_id`, `action` (always `transcript_read`), `at` (RFC 3339 text), `page_before` (nullable integer), and `page_limit` (integer). Collaboration writes one row per successful response in the same transaction as the transcript read. No route exposes the audit table, and it contains no message bodies. These audit rows are the console's only writes to tenant data.

## Schema and migrations

The following table records the v6 baseline after #6 merged. [#45](https://github.com/Jianyu010/TeamUp/issues/45) later introduced a **v7** child table for additional room message recipients in order. Existing messages still use `mention_member_id` for the first or only recipient. Retention cleanup deletes the additional recipient rows along with the room message. The operator's `ConsoleMessage` continues to show the first-recipient field. Room member reads follow [Client–Server v1](client-server-v1.md#multiple-explicit-recipients-for-room-messages-45). The current **v8** adds the [member retirement flag](client-server-v1.md#member-retirement-and-room-departure-47).

The v6 baseline builds on released v1 and v2. The #6 history changes (`history_policy`, `created_at`, and `id_counters`) produced a v3 draft. The #3 DM changes produced **v4** with a `dms` table, nullable `messages.room_id` with an exactly-one-non-null constraint, unique retry indexes scoped by conversation type, and `dm` in `inbox_events.kind`. The console layer brought this to **v5** by adding the session `purpose` column, `operator_grants`, `room_activity`, and `transcript_access_audit`. The final #6 retention layer added nullable `expired_through_message_id` watermarks to rooms and DMs for **v6**. Both v3 drafts report `user_version` 3. The migration distinguishes them by their tables and columns, and the runtime does not depend on this distinction. A `purpose` column in `account_sessions` and an `operator_grants` table identify the released #5 authentication preview v3 (`e495544`, with no history columns). The #6 history v3 has `rooms.history_policy` and lacks the operator fields. The DM column in `messages` identifies #3 v4. The standalone #6 retention worktree once used v5 for the same two watermark columns. That variant was never released, so the migration treats every database at `user_version` 5 as the released console v5. It applies each row's steps in order in one write transaction, backfills the activity projection, and sets `user_version = 6` at the end.

| Starting version | Steps |
| --- | --- |
| 0 (fresh) | Create the full v6 schema directly, including DM tables, history columns, counters, `purpose`, grants, the activity projection, the audit table, and both retention watermarks. There are no rows to backfill. |
| 1 | Add the account schema with `purpose` in `account_sessions`. Add history columns and seed counters, rebuild for DMs, add the console schema, and add the retention watermark columns in place to reach v6. |
| 2 | Add history columns and seed counters, then rebuild for DMs. Add `purpose` to `account_sessions` with enrollment for existing rows. Add the console schema and the retention watermark columns in place to reach v6. |
| 3 (#6 history v3 draft) | Rebuild for DMs, add `purpose` to `account_sessions`, add the console schema, and add the retention watermark columns in place to reach v6. |
| 3 (#5 authentication preview v3, `e495544`) | Add the missing #6 history columns and seed counters before rebuilding for DMs. Add only the read-path schema (projection and audit tables), followed by the retention watermark columns in place to reach v6. Preserve `purpose`, existing sessions, `operator_grants`, and room data without regranting access or resetting state. |
| 4 (#3 DM v4) | Add `purpose` to `account_sessions`, add the console schema, and add the retention watermark columns in place to reach v6. |
| 5 (released console v5) | Add both retention watermark columns in place with one `ALTER TABLE` each for rooms and DMs. NULL means cleanup has never run for that room or DM. Preserve `purpose`, grants, the projection, audits, and existing data when moving to v6. |

The DM rebuild follows the #3 contract. Rebuild the message table so exactly one of `room_id` and `dm_id` is non-null, then attach the retry indexes scoped by conversation type. Existing rows keep their IDs, ordering, and column values. Rebuild `dms` in its released v4 shape and add the retention watermark later. Adding history columns to preview v3 rooms defaults them to `since_joining`, consistent with the old room creation behavior. All upgrade paths then backfill activity. Backfill and runtime collection both exclude DM rows with `room_id IS NULL`. This merge brings in #6 retention cleanup (`--room-retention-hours` and `--dm-retention-hours`, `expire_due` and `expire_due_dms`, and the `expired_through_message_id` boundary). When the cleanup transaction deletes expired room messages in the `room_id IS NOT NULL` scope, the foreign key described above clears the associated links in the activity projection.

## Implementation steps

### Stage 2 (implemented in e495544)

1. ~~Collaboration schema v3 with `purpose` and `operator_grants`.~~ (Merged into v5.)
2. Four account administration CLI subcommands.
3. Single-transaction authorization in `list_console_rooms`.
4. Optional `purpose:"console"` on `POST /v1/auth/login`, the `GET /v1/console/rooms` route, 403 `forbidden` in the error table, and static hosting for the three routes.

### Stage 3 (implemented)

5. ~~v4 schema and migration (see the table above), with `room_activity`, `transcript_access_audit`, and a backfill for existing data.~~ (Revised to v5 after the #3 merge, below.)
6. Write projections from join, Board acknowledgment, send, and delivery acknowledgment within the four owning transactions. Idempotent retries produce no duplicates.
7. Add Collaboration reads for `console_room_detail` (roster and `transcript_allowed`), `console_timeline` (ascending pagination with `after` and `limit`), and `console_transcript` (latest and earlier pages with `before`, `limit`, `has_more`, and same-transaction auditing). Populate `last_activity` from the room's activity projection.
8. Mount the three Gateway routes. All successful `/v1/console/*` responses send `Cache-Control: no-store`. Transcript uses the canonical #6 message records and owner, with the same SQL projection plus the registered role at read time and a mandatory `m.room_id = ?` filter. Member endpoint shapes stay compatible.

### #3 Server merge (implemented)

9. The v5 schema and migration (in the table above) add the console layer on top of #3 DM v4. They support fresh databases, v1, v2, both v3 variants, and #3 v4. For preview v3, add the #6 history columns and seed the counters before rebuilding for DMs. Keep the preview's `purpose`, sessions, grants, and room data.
10. Exclude DMs from the activity projection. Both migration backfill and runtime activity collection filter by `room_id IS NOT NULL`. DM sends and delivery acknowledgments never append `room_activity` rows. Transcript continues to use the `m.room_id = ?` filter, `before` and `has_more` for pagination, a separate `transcript_read` grant, and read auditing without message bodies.

### #6 retention runtime merge (implemented in this merge)

11. The final v6 schema and migration add retention watermarks to rooms and DMs in place on top of console v5. The supported starting points are fresh databases, v1, v2, both v3 variants, #3 DM v4, and console v5. The separate #6 retention worktree once marked the same two columns as v5. Since that variant never shipped, it needs no compatibility branch.
12. Retention cleanup works alongside the console. The cleanup transaction first removes inbox references, then deletes the room messages. In the `room_id IS NOT NULL` scope, the existing foreign key sets the activity projection's expired links to null and leaves the event rows in place. Cleanup leaves audits, permissions, and the DM scope intact. `--room-retention-hours` and `--dm-retention-hours` enable their respective features independently and share a single maintenance loop. Omitting both disables scheduled cleanup. See the [conversation history module contract](conversation-history.md) for runtime behavior.

### Follow-up (separate contract revision)

13. Room member pagination for the #6 search worker and any effect of retention cleanup on audits. The behavior that clears expired message links in Timeline is already defined and implemented, as described in the activity projection section.

## Test procedure

Server contract tests are in `tests/server/src/teamup_server/test_console_server.py`. The stage 2 cases are retained and stage 3 adds coverage.

1. (Stage 2) An account without `console_read` can sign in with `purpose:"console"`, but `/v1/console/rooms` returns 403 `forbidden`. Invalid, expired, or revoked console sessions return 401, including sessions revoked by deactivation or password reset.
2. (Stage 2) Verify purpose isolation in both directions. A console session token used on `POST /v1/members` or ordinary member APIs returns 401. Passing it as `member_token` returns 422. Using an enrollment-purpose login token, member token, or legacy enrollment key on a console route returns 401. Logging in without `purpose` preserves existing behavior.
3. (Stage 2, updated in stage 3) Migrate an existing v2 database. Old sessions still work for enrollment. The console rejects them, and `user_version` becomes **8**.
4. (Stage 2) Verify `grant-console` and `revoke-console` through the CLI by switching idempotently between 403 and 200. Verify that `grant-transcript` and `revoke-transcript` are idempotent and report errors for missing accounts or rooms.
5. (Updated in stage 3) Verify that the room list sorts stably by `room_id` and contains exactly `room_id`, `name`, `member_count`, and `last_activity`. `last_activity` is the latest activity projection, or null if there is no activity. Reading the room list creates no inbox events, SSE hints, or room writes.
6. (Stage 2) Check the three static routes and their security headers. Other `/console/…` paths return 404, and unsupported methods return 405.
7. (Stage 3) Check the roster response fields `room`, `members`, and `transcript_allowed`. `board_state` reflects acknowledgment, `harness_state` is always `unknown`, ordering is stable by `member_id`, and `last_activity` reflects the member's latest activity. Missing rooms return 404.
8. (Stage 3) Check all four Timeline activity shapes and verify that none includes the message body. Pagination uses `after` and `limit` in ascending order, with a short page ending the list. Retried joins, Board acknowledgments, sends, and delivery acknowledgments produce no duplicate projection rows.
9. (Stage 3) Omit `before` to read the latest page in ascending order. Supply it to read an earlier page, and verify that `has_more` is accurate. A `before` greater than SQLite's maximum integer returns the latest page successfully and records a null `page_before` in the audit. `sender_role_description` reflects the registered role at read time. Older messages without timestamps return null `created_at`. Every successful response writes one audit entry in the same transaction, with the owner, room, action, pagination parameters, and timestamp, and without the body.
10. (Stage 3) An operator with metadata-only access cannot read message bodies. Timeline omits them, and the transcript returns 403. A room-level `transcript_read` grant applies only to that room. Other rooms return 403. Transcript checks for a missing room after `console_read` and before room-level authorization. An unknown room returns 403 without `console_read` and 404 with it. An existing room that the operator lacks access to still returns 403. After grant revocation, using an old cursor returns 403. A disabled or expired session returns 401. Console reads create no inbox events or SSE hints.
11. (Stage 3) Upgrade v2 and #6 history v3 samples and verify the activity backfill, including its specified order and null `at` fields. Message IDs and existing #6 semantics are preserved.
12. (Stage 3) All successful responses send `Cache-Control: no-store`.
13. (#3 merge) Upgrade a representative database from the released #5 authentication preview v3 (`e495544`, with `purpose` and `operator_grants` but no history columns). Verify that the `console` and `enrollment` purposes, sessions, `console_read` and `transcript_read` grants, and room data are preserved. Operators with existing grants can sign in and read rooms and transcripts without new grants. The added history layer defaults existing rooms to `since_joining`. DMs work after the rebuild, and IDs continue from the previous maximum. Upgrade a #3 DM v4 sample and verify that acknowledged DM deliveries do not backfill room activity, transcripts contain only room rows, DM history is still available only to its participants, and IDs continue from the previous maximum.
14. (#3 merge) Even with `console_read` and the room's `transcript_read`, an operator sees no DM text or DM `message_id` in responses from rooms, rosters, Timeline, or transcripts after DMs are enabled, exchanged, and acknowledged. DM sends and acknowledgments create no `room_activity` rows.
15. (Stage 3, #5/#6 compatibility) In a temporary database, write a room message and activity including delivery acknowledgment. Use a direct SQL connection within the test with foreign keys enabled and clear the inbox references before deleting the message, reproducing the #6 cleanup transaction. Verify that deletion succeeds and Timeline retains `kind`, `at`, and actor with a null `message_id`. Links for retained messages still work. The schema CHECK still rejects join or Board acknowledgment activities with a `message_id`.
16. (#6 retention runtime merge) Verify actual retention cleanup with an authenticated console. Start a real Server with `--room-retention-hours` and a sample in the released console v5 shape (v6 without either watermark). Upgrade to the current schema and wait for the cleanup commit. Verify that expired room bodies and unacknowledged deliveries disappear, Timeline retains the message and delivery metadata with null `message_id`, and member fetches or acknowledgments for expired events return 404 `not_found`. Transcript reads continue to record audits without bodies. DM bodies and DM read permissions are unaffected. The `user_version` is 8.

Console UI regression tests in `tests/server/src/teamup_server/console/test_console_ui.py` cover Timeline and Chats states after pagination failures and retries with the original cursor.

## Acceptance criteria

### Stage 2 (accepted)

- Login with `purpose:"console"`, grants, and room metadata reads through `/v1/console/rooms` follow this contract. Session purposes are isolated in both directions.
- The Gateway serves exactly the three same-origin bundle paths with a strict CSP. Arbitrary file serving is unavailable, and the UI provides no write controls.
- Opening the console creates no chat activity. The Server checks authorization within the transaction that uses it.

### Stage 3

- Timeline displays room activity metadata, and Chats displays the selected room's conversation if the operator is authorized.
- Reading message bodies requires a transcript grant separate from metadata access. Each successful transcript page leaves an audit record that omits the body.
- Revocation and deactivation take effect on the next page. Console reads leave member inbox, SSE, and write behavior intact.
- Migrations cover fresh databases, v1, v2, both v3 variants (#6 history draft and released #5 authentication preview), #3 DM v4, released console v5, retention watermark v6, and multi-recipient v7. The current `user_version` is 8.
- The console excludes DMs. Authorized operators cannot trace DM text or a DM `message_id` through Timeline or transcripts. DM sends and acknowledgments generate no room activity. Delivered and tested in the #3 merge.
- #6 retention cleanup is compatible with the console. Expiration in rooms removes message bodies and unacknowledged deliveries. The foreign key clears Timeline links and preserves activity rows. Auditing and DM scope are unaffected. Delivered and tested in the #6 retention runtime merge.

## Decisions for review across owners

1. ~~Pilot prerequisite~~. The worktree includes the pilot integration and follows Model A.
2. ~~Effect of the browser holding `account_token` on scope~~. Limit scope at the session level with `purpose:"console"`.
3. ~~403 `forbidden` and `before` pagination parameter~~. Delivered with the stage 3 routes.
4. ~~Audit rows include pagination parameters but no bodies~~. Implemented with `page_before` and `page_limit`.
5. ~~Static asset hosting~~. Implemented.
6. ~~Activity projection table and backfill strategy~~. Implemented by backfilling from canonical rows and setting `at` to null when no timestamp is available.
7. The coordinator added the entry to the index in `docs/modules/README.md` in this stage.
