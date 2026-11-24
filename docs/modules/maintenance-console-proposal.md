# Operator Maintenance Console Contract Proposal

**Design proposal for TeamUp #32, part of #17.** Related to the [#5 Control Console](https://github.com/Jianyu010/TeamUp/pull/12) and the #6 conversation-history boundary. Base revision: `94261a7`.

This is a contract proposal only. It adds no production code, routes, dependencies, telemetry exporters, or deployment changes. The repository has no approved telemetry endpoints today, so every endpoint and metric below requires owner review before implementation. The proposal deliberately uses the existing JSON API style and existing operator identity rather than introducing Prometheus, OpenTelemetry, StatsD, or another side channel.

The #5 Control Console is not present at the base revision. This proposal assumes the #5 operator boundary described in [Control Console product behavior](../specs/collaboration-surfaces.md#control-console): an account login session with `purpose:"console"`, server-side grants, and read-only console routes. Until #5 lands, this document remains a dependency-aware proposal and must not be implemented ahead of it. See [Sequencing](#sequencing-and-open-review).

## Scope

The maintenance console answers four narrow operational questions:

1. Is the Server host and process healthy enough to serve TeamUp?
2. Is Gateway transport healthy: request volume, errors, latency, and active SSE connections?
3. Is Collaboration healthy: database size, transaction pressure, schema version, and aggregate domain load?
4. Which TeamUp room is contributing observable load, without reading its transcript?

It is not a general administration console. It cannot publish boards, change grants, stop members, join rooms, send messages, read room transcripts, read DMs, or inspect host files. Emergency stop and grant management keep their existing owners and entry points.

## Ownership and authority

Every quantity in this contract comes from the component that owns it. The maintenance read API is an assembly and presentation layer; it is not a second owner of business state.

| Source | Owner | Public read port | Authoritative data |
| --- | --- | --- | --- |
| Operator identity and grants | Collaboration, through the #5 account boundary | Existing login route and server-local account CLI | Accounts, sessions, `console_read`, `transcript_read`, and the new maintenance grants |
| Maintenance API | Gateway | The read-only `/v1/console/...` routes in this proposal | No state; it rechecks the session and grants through Collaboration and forwards owner-provided observations |
| Host observations | Deployment host probe | Host metrics read port | Process and host operating-system counters only |
| Gateway observations | Gateway | Gateway metrics read port | Request, error, latency, and active-stream counters from its own process |
| Collaboration observations | Collaboration | Collaboration metrics read port | Database health and aggregate domain counts from the business database |
| Redacted logs | Each emitting component owns emission; the deployment log sink owns storage and query | Redacted log query port | Already-redacted structured operational entries only |

Authority rules:

- **No data reads bypass owners.** The maintenance API must not open the business SQLite database, tail component log files, read `proc` entries for a component other than through the host probe, scrape Gateway internals, or inspect request bodies to derive metrics.
- **Owner checks every read.** Collaboration and Gateway recheck the console session, account status, and required grant on each request. A cached snapshot, console projection, or prior successful response is never authorization.
- **No authoritative cache.** The maintenance API may display a short-lived owner sample, but it does not persist grants, room membership, or transcript data and does not use a sample for authorization.
- **No content path.** Metrics, load summaries, and redacted logs must not become an alternate transcript or DM read path. Room transcripts remain controlled by the #5 `transcript_read` grant and the [conversation-history contract](conversation-history.md). DMs remain unavailable through the maintenance console.
- **No cross-owner coupling.** Gateway does not read Collaboration tables. Collaboration does not inspect Gateway sockets. The host probe does not read TeamUp databases or logs. The log sink does not read TeamUp databases.

## Permissions

The identity boundary is reused from #5: the operator signs in through the account login with `purpose:"console"`, and only a console-purpose session may call `/v1/console/*`. Member tokens, enrollment keys, and registration-purpose account tokens remain invalid for every console and maintenance route.

All maintenance routes live under the existing `/v1/console/` family. They therefore keep the #5 rule that every console route requires a valid console session and the account-level `console_read` grant. New grants narrow access further; none of them replaces `console_read`.

| Grant | Scope | Allows | Does not allow |
| --- | --- | --- | --- |
| `console_read` | Account | Existing #5 room metadata and the room identity needed for a per-room load query | Any message body, transcript, or DM |
| `maintenance_read` | Account | Host, Gateway, and Collaboration health summaries; per-room load when combined with `console_read` | Logs, room transcript, DM, grant writes, or any mutating action |
| `logs_read` | Account | Redacted operational log query | Message, board, or DM content; raw request or response payloads; credentials |
| `transcript_read` | Account × room (#5, unchanged) | Reading the selected room's transcript through the #5 transcript route only | Maintenance metrics, logs, DM, or any other room |
| DM transcript grant | Out of scope | Nothing in this proposal | A future DM grant must be account × conversation and separately approved; it is never implied by any room or maintenance grant |

Grant management remains server-local, following the #5 pattern. The proposal adds, for later review, `grant-maintenance`, `revoke-maintenance`, `grant-logs`, and `revoke-logs` subcommands on the existing account CLI. The network API has no grant-write route. Grants are stored and checked by Collaboration; names and storage shape require review with the #5 owner.

Room transcript access and DM access are intentionally separate:

- A maintenance operator with `transcript_read` for one room still sees no transcript through a maintenance route. Transcript reads continue through the #5 route and its audit.
- A maintenance operator without `transcript_read` can still see aggregate room load. The load response contains no message text, sender identity, role description, board text, or transcript cursor.
- No route in this proposal can discover or read a DM. DM counts and DM activity are not exposed as aggregate metrics, because even their existence is participant-scoped.

Error semantics reuse #5 and [Client–Server v1](client-server-v1.md): an invalid, expired, revoked, or wrong-purpose session is 401 `unauthorized`; a valid console session missing the required grant is 403 `forbidden`; an unknown or non-team room is 404 `not_found`; bounded query misuse is 422 `invalid_request`; login rate limiting keeps 429 `rate_limited`.

## Metrics model

The console presents point-in-time observations, not a monitoring system. Each source returns a bounded JSON snapshot with a common envelope.

| Field | Meaning |
| --- | --- |
| `source` | `host`, `gateway`, or `collaboration` |
| `sampled_at` | UTC RFC 3339 time when the owner collected the values, not when the maintenance API served them |
| `sample_interval_seconds` | Expected refresh interval; proposed default 15 seconds |
| `stale_after_seconds` | Age above which the sample is stale; proposed default 45 seconds |
| `unavailable_after_seconds` | Age above which the sample is unavailable; proposed default 90 seconds |
| `state` | `fresh`, `stale`, or `unavailable` |
| `values` | Counters and gauges for that source |
| `rates` | Optional derived rates, or null when they cannot be computed honestly |

State and time rules:

- **Fresh:** `generated_at - sampled_at` is at most `stale_after_seconds`.
- **Stale:** the last owner sample is older than `stale_after_seconds` but not older than `unavailable_after_seconds`. The last values may be shown with an explicit stale label; they are not current capacity facts and must not feed a rate.
- **Unavailable:** there is no owner sample, the source is not deployed, the owner read fails, or the sample is older than `unavailable_after_seconds`. Numeric fields are null rather than guessed.
- **No sample, no guess.** A missing host probe, disabled metric family, permission failure, or unsupported platform yields `unavailable`. The console never synthesizes host, Gateway, or Collaboration observations from a sibling source.
- **Monotonic intervals.** Durations use the source's monotonic clock. Wall-clock timestamps are UTC RFC 3339 and are display and correlation data only.

Counters and rates:

- **Counters** are monotonic totals since the owning process instance started, or since the owning database was created where explicitly stated. Examples: HTTP requests, error responses, CPU seconds, transactions, busy errors, login failures.
- **Gauges** are values at the sample instant. Examples: resident memory, open file descriptors, active SSE streams, current stored messages, pending inbox events.
- **Rates are derived, never authoritative.** The maintenance API may expose a rate only when it has two fresh samples from the same source instance with a known positive interval. The response includes `window_seconds` and the number of samples. A rate is null on the first sample, after a source restart, when either sample is stale, or when the counter decreased.
- **Restart detection.** Counters reset when a process restarts. A lower counter value starts a new baseline and never produces a negative rate. Collaboration totals that can shrink because of retention are gauges, not counters.
- **No precomputed history.** The console does not store a time series. It may show the latest rate or a short in-memory comparison, but it does not become a monitoring database or an audit record.

## Minimal metric set

The set below is the minimum needed to answer the four scope questions with existing component state and standard-library operating-system observations. It is not a general-purpose metric catalogue.

### Host

Owner: deployment host probe. These observations are available from the operating system without a TeamUp business-database read and without a new third-party dependency.

| Metric | Type | Notes |
| --- | --- | --- |
| `process_uptime_seconds` | Gauge | Server process uptime |
| `process_cpu_seconds_total` | Counter | User plus system CPU time for the Server process. An observed interval can show process CPU usage with the unit and scope labeled. |
| `host_cpu_busy_fraction` | Gauge | Optional host-wide measurement from two OS samples taken over a known interval |
| `process_resident_memory_bytes` | Gauge | Resident set size |
| `host_memory_used_bytes`, `host_memory_total_bytes` | Gauge | Optional host or container memory observations, labeled with the source's scope |
| `process_open_file_descriptors` | Gauge | Current descriptor count |
| `db_volume_free_bytes` | Gauge | Free space on the volume holding the business database |
| `db_volume_total_bytes` | Gauge | Total size of that volume |
| `load_1m`, `load_5m`, `load_15m` | Gauge | Optional host-wide load averages; label them host-wide because a shared host is not attributable to TeamUp |

Host metrics exclude process arguments, environment variables, hostname, command lines, per-file paths, container identifiers, network peers, and any database content.

### Gateway

Owner: Gateway. These values come from its own request and connection state. Labels are bounded to route templates, method, status class, and error code; room IDs and member IDs are not aggregate metric labels.

| Metric | Type | Notes |
| --- | --- | --- |
| `http_requests_total` | Counter | By route template, method, and status class (`2xx`, `4xx`, `5xx`) |
| `http_request_duration_seconds_total` | Counter | Sum of handler durations by route template |
| `http_request_duration_count` | Counter | Handler count corresponding to the duration sum. The pair supports an average. A p95 requires bounded duration histograms and a minimum sample count. |
| `http_error_responses_total` | Counter | By published error code, including authentication, authorization, validation, conflict, rate-limit, and internal categories |
| `active_http_requests` | Gauge | Requests currently being handled |
| `active_sse_streams` | Gauge | SSE responses currently open |
| `sse_connections_total` | Counter | SSE subscriptions opened since process start |

The Gateway must not classify by raw path, query string, room ID, member ID, client IP, or user agent. Search terms and message text never become metric labels.

### Collaboration

Owner: Collaboration. Aggregate database and domain observations are read through its own port. Counts are safe aggregates, not rows or bodies.

| Metric | Type | Notes |
| --- | --- | --- |
| `db_size_bytes` | Gauge | Main database plus WAL where present |
| `schema_version` | Gauge | Current Collaboration schema version |
| `sqlite_busy_errors_total` | Counter | Busy or locked failures surfaced at the Collaboration boundary |
| `transactions_total` | Counter | Transactions attempted |
| `transaction_duration_seconds_total` | Counter | Sum of transaction durations |
| `members_stored` | Gauge | Current member rows; no identities |
| `team_rooms_stored` | Gauge | Current team-room rows; DMs excluded |
| `messages_stored` | Gauge | Current stored room messages; may fall after retention, so a gauge |
| `pending_inbox_events` | Gauge | Currently unacknowledged room inbox rows |
| `oldest_pending_age_seconds` | Gauge | Age of the oldest pending room delivery with a usable timestamp, or null |
| `login_failures_total` | Counter | Failed login attempts, without username |
| `rate_limited_total` | Counter | Requests rejected by login rate limiting |
| `authorization_denials_total` | Counter | By bounded reason such as missing session, wrong purpose, or missing grant |

Collaboration metrics exclude message text, sender identity, role descriptions, board text, client retry keys, credentials, token digests, raw SQL, database file paths, DM counts, and per-member activity. DM existence remains participant-scoped and is not exposed here.

### Per-room load

Owner: Collaboration. Each response is scoped to exactly one team room named in the path. The operator must hold `console_read` and `maintenance_read`. The response is aggregate metadata and contains no transcript or member roster.

| Field | Type | Notes |
| --- | --- | --- |
| `room_id` | UUID string | The requested team room |
| `sampled_at` | UTC RFC 3339 | Collaboration sample time |
| `state` | `fresh`, `stale`, or `unavailable` | Same rules and thresholds as the common envelope |
| `member_count` | Gauge | Current number of room members |
| `messages_stored` | Gauge | Current messages stored for this room; may fall after retention |
| `pending_inbox_events` | Gauge | Current unacknowledged events for this room |
| `oldest_pending_age_seconds` | Gauge | Age of its oldest pending delivery with a usable timestamp, or null |
| `last_activity_at` | UTC RFC 3339 or null | Latest Server-recorded room activity; null when the room has no timestamp yet |
| `load_rates` | Object or null | Optional derived rates from two fresh Collaboration samples; null otherwise |

The load response must not include any sender or member identity, role description, board text, message text, message ID, search snippet, transcript cursor, or DM. A room with a DM identifier or a non-team identifier is not a valid target and returns 404 `not_found`, matching the #5 room set. The Client can use a page from the #5 room list and read load for its visible rows. A bounded batch route can be reviewed if per-row reads become a measurable bottleneck.

## Routes and shapes

These routes are proposed for the same Gateway provider and `/v1/console/` family as #5. None is implemented by this change.

| Method and path | Required grants | Success result |
| --- | --- | --- |
| `GET /v1/console/maintenance/health` | `console_read` + `maintenance_read` | `{"generated_at":"...","sources":{"host":SourceSnapshot,"gateway":SourceSnapshot,"collaboration":SourceSnapshot}}` |
| `GET /v1/console/rooms/{room_id}/load` | `console_read` + `maintenance_read` | `{"room":RoomLoad}` |
| `GET /v1/console/maintenance/logs?since=...&until=...&component=...&limit=...` | `console_read` + `logs_read` | `{"state":"available"|"unavailable","entries":[RedactedLog],"truncated":boolean}` |

`SourceSnapshot` carries the common envelope and a `values` object; optional `rates` is null unless it satisfies the two-sample rule. `generated_at` is the maintenance API response time, not a new sample. Health and per-room load are not audited because they contain no message or board content. Unknown routes remain 404, non-GET methods remain 405, and no route is added to the member API.

The log query is a bounded time window, proposed maximum one hour per request and at most 500 entries. It has no free-text search, no regular expression, and no cross-component content join. If the requested window predates retention, the response reports `truncated` rather than reconstructing old entries.

## Redacted logs

Logs are the most sensitive maintenance surface because ordinary application logs can accidentally capture request content. The proposal uses both source-side redaction and a second allowlist pass at query time.

Allowed fields are deliberately narrow: timestamp, severity, component, stable event code, route template, method, status class, error code, duration in milliseconds, opaque request or correlation identifier, count, and an opaque room ID only when a per-room operational event cannot be expressed otherwise.

Forbidden fields include all message text, DM bodies, board rules or protocol text, search queries, sender or member names, role descriptions, tokens, passwords, authorization headers, cookies, session identifiers, token digests, raw request or response bodies, raw paths with room identifiers, query strings, client IP addresses, user agents, filesystem paths, environment variables, and process command lines.

Additional rules:

- Components redact before emission; the log sink never receives a raw body and the maintenance API never reads a component log file directly.
- The query-time allowlist drops unknown fields instead of forwarding them.
- Log queries are audited by Collaboration or the log owner with operator `owner_id`, requested window, component filter, result count, and time. The audit does not copy log entries or any redacted content.
- A log source that is not deployed or not readable returns `state:"unavailable"` and no entries.
- Logs are not a transcript substitute. A `transcript_read` grant does not grant logs, and `logs_read` does not grant transcript access.

## Acceptance matrix

| ID | Case | Preconditions and action | Expected |
| --- | --- | --- | --- |
| A1 | Valid operator identity | Account enabled, console-purpose login, `console_read` + `maintenance_read`; call health | 200 with all three source envelopes and no 401 or 403 |
| A2 | Missing new grant | Valid console session and `console_read`, no `maintenance_read`; call health or room load | 403 `forbidden`; no metric body |
| A3 | Wrong credential class | Member token, enrollment key, or registration-purpose account token; call any maintenance route | 401 `unauthorized`; no data |
| A4 | Revoked or disabled operator | Disable the account or revoke the console session; retry with the old token | 401 `unauthorized` on the next request; no cached success |
| A5 | Console cannot enroll | Use the console-purpose token at `POST /v1/members` | 401 `unauthorized`, preserving #5 cross-purpose isolation |
| A6 | Grant separation | `console_read` + `maintenance_read` but no `logs_read`; call logs | 403 `forbidden`; `logs_read` alone without `console_read` is also 403 |
| A7 | Transcript separation | Operator has `transcript_read` for the room but no `maintenance_read`; call room load or health | 403; a maintenance grant never adds transcript fields to any maintenance response |
| A8 | DM separation | A DM exists; list team rooms, query the DM identifier as a room load, and query logs | DM is absent from room lists and metrics; DM identifier returns 404; no DM body or existence appears in logs |
| M1 | Common envelope | Read health | Each source has `sampled_at`, `sample_interval_seconds`, `stale_after_seconds`, `unavailable_after_seconds`, `state`, and values; `generated_at` is present |
| M2 | Counter behavior | Take two fresh samples with a known interval | Counters do not decrease without a restart; any provided rate has `window_seconds` and two samples |
| M3 | Insufficient rate basis | Request a rate with one sample, or with a stale first sample | Rate is null; no guessed value and no negative rate |
| M4 | Restart | Restart the owning process between samples | Counters reset to a new baseline; rate is null until a second post-restart sample |
| M5 | Stale transition | Stop refreshing an owner after a fresh sample and wait past `stale_after_seconds` | `state` becomes `stale`; last values may be shown with the label; rates are null |
| M6 | Unavailable transition | Keep the owner stopped past `unavailable_after_seconds`, or omit the host probe | `state` becomes `unavailable`; numeric fields are null, not invented from another source |
| M7 | No content in metrics | Send a room message, publish board text, create a DM, and search with a unique marker; read health and room load | No marker, message text, board text, sender identity, DM data, or search term appears in any metric response |
| L1 | Redaction | Send messages and DM text containing unique markers; log a token-like marker and a search term; query logs | Markers, text, credentials, headers, queries, IPs, paths, and bodies are absent; only allowlisted fields remain |
| L2 | Log access audit | Query logs with `logs_read` | An audit entry records operator, window, component, count, and time, with no copied log content |
| L3 | Log bounds | Request a window over the maximum span or over the limit | 422 `invalid_request`; no unbounded read |
| R1 | Per-room scope | Query load for one team room | Response contains only that `room_id`; no transcript, roster, member IDs, message IDs, or DM fields |
| R2 | Non-team target | Query load for a nonexistent room, a DM, or an arbitrary UUID | 404 `not_found`; no aggregate or existence leak |
| R3 | Bounded room loading | View a paginated room list and request load for the visible rows | Each load response is checked separately and represents one room |
| B1 | No bypass | Static dependency check and contract test against the maintenance provider | No import of Collaboration internals, direct business-SQLite access, direct log-file read, or Gateway table access from the maintenance API |
| B2 | Owner recheck | Revoke `maintenance_read` or `logs_read` between two requests with a valid session | The next owner check returns 403; no cached grant or projection authorizes the read |
| S1 | #5 sequencing | Base revision has no #5 console identity or grants | This proposal is not implemented; #5 must land its purpose, grants, route family, and room set first, then this contract is reviewed against that code |
| S2 | No new dependency | Inspect the implementation proposal | No Prometheus, OpenTelemetry, StatsD, log shipper, or other new production dependency is required or approved by this document |

## Sequencing and open review

The following order is required:

1. **#5 identity and console foundation.** Land the account login purpose, account lifecycle, server-local grants, `console_read`, room-scoped `transcript_read`, 403 semantics, and the team-room metadata route. This proposal reuses that boundary and must not create a second operator identity.
2. **This contract review.** Approve the new `maintenance_read` and `logs_read` grants, route shapes, metric names, sample and staleness thresholds, redaction allowlist, and per-room load scope with the Gateway, Collaboration, host/deployment, and log owners.
3. **Owner telemetry ports.** Each owner exposes only its own read port. No aggregator may read another owner's tables, sockets, files, or process internals.
4. **Implementation and contract tests.** Add the routes, redaction, audit, dependency checks, and the acceptance matrix above in a separate change after approval.

Because #5 is pending at base `94261a7`, references to #5 reuse are conditional. If #5 changes the console purpose, route family, `console_read` semantics, or grant configuration, this proposal must be reconciled before coding. If #5 is not accepted, this proposal is blocked rather than creating a parallel operator boundary.

Open choices for review, with proposed defaults only:

- Final staleness thresholds. Proposed: 15-second sampling, 45-second stale boundary, 90-second unavailable boundary, aligned with the MVP heartbeat shape in [MVP design](../specs/mvp.md).
- Final grant names and CLI subcommand spelling. Proposed: `maintenance_read`, `logs_read`, `grant-maintenance`, `revoke-maintenance`, `grant-logs`, `revoke-logs`.
- Whether rate computation belongs in the owner snapshot or the maintenance API. Proposed: owner supplies counters and gauges; the API derives and labels a rate only when two fresh same-instance samples exist.
- Log retention and maximum window. Proposed: deployment-owned retention with a one-hour query cap and a 500-entry cap.
- Whether host load averages are included when the host is shared. Proposed: include only with an explicit host-wide label; omit if attribution would be misleading.

Review owners: Gateway for routes, errors, and transport metrics; Collaboration for identity reuse, grants, database and per-room aggregates, audit, and transcript/DM separation; deployment/host owner for the host probe; deployment/log owner for redacted log storage and query. Per the [development protocol](../protocols/README.md), new fields, permissions, and failure semantics require owner agreement before code. These fields remain proposals under [AGENTS.md](../../AGENTS.md).
