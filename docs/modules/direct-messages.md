# Direct Messages

**The #3 contract is agreed and implemented.** The Client and MCP shipped their changes with this contract. The #3 Server worktree delivered `dms` storage, three routes, the inbox `kinds` parameter, and `kind: "dm"` events. The #6 DM retention storage worktree delivered the `dms` watermark column, the `expire_due_dms` batch cleanup entry point, and retention metadata on reads. The #6 retention runtime integrated shared maintenance scheduling and the production `--dm-retention-hours` configuration. This integration finalized schema v6 on top of the console baseline.

The Server Gateway provides the contract and Collaboration owns the domain state. The Client, local MCP plugin, and CLI consume it. This contract adds DM capabilities to the published room interfaces only as specified below. [Direct messages #3](https://github.com/Jianyu010/TeamUp/issues/3) · [Conversation history #6](../specs/conversation-history.md) · [Product behavior](../specs/collaboration-surfaces.md) · [Client–Server v1](client-server-v1.md) · [MCP–Client v1](mcp-client-v1.md)

## Scope and boundaries

- A DM is a conversation between exactly two members of one team. Each unordered member pair has one conversation. The recommended storage order is `min`/`max`. Reopening the conversation, from either member's side, returns the same DM.
- Find a teammate using `teamup_find_members`. Search by role and use the returned `member_id` to open a DM.
- Only the two participants can send and read messages and fetch events for that DM. The service always derives the caller's identity from the current token and rechecks participation on every operation. A client field cannot override that identity. Other team members cannot query the conversation or learn whether it exists.
- DMs have no Board. Participants can send and read without acknowledging one.
- DMs stay outside the Room Directory, room search, and Control Console. An administrative grant to read room transcripts does not grant DM access.
- This release supports two-person conversations. Closing, leaving, and transferring a DM are outside its scope. When opening a DM, a peer whose account has been disabled is treated as unknown and returns 422 `invalid_peer`. Active participants can still read stored history for an existing conversation. Sending to a peer whose account has been disabled returns 404 `not_found` under the transaction and error rules below.

Member retirement (#47) follows the same rules for an unavailable peer. An active participant can read an existing DM. Opening a DM with the retired member returns 422 `invalid_peer`, and sending a new message to them returns 404 `not_found`. An identical retry of a previously committed message returns the original receipt.

## Data shapes

| Name | JSON fields |
| --- | --- |
| DM | `dm_id` and `peer_member_id`, both UUID strings. |
| DM Message | `message_id` as a positive integer, `dm_id`, `sender_member_id`, `client_message_id`, `text`, and `created_at` as an RFC 3339 UTC timestamp string. All DM rows were written with a timestamp, so it is always non-null. |
| Event (extension) | Keeps `event_id`, `kind`, and `message`. Adds `dm` as a valid `kind`, in which case `message` is a DM Message. |
| Notice (local extension) | A local DM Notice has five typed fields, `{event_id, kind:"dm", dm_id, message_id, sender_member_id}`, and omits the body. An existing `mention` Notice still outputs the original four fields `{event_id, room_id, message_id, sender_member_id}`. |

- DMs and rooms draw `message_id` values from the same global allocator. IDs are unique across both families, increase strictly in commit order within a conversation, and are never reused. Room and DM messages are separate canonical record families within Collaboration, with one copy of the body per record. The two event families also share the global delivery allocator from #6.
- This DM release keeps the room Message wire format. The separate [conversation history, search, and retention changes](conversation-history.md) settle the timestamp question for room reads by adding `created_at` to the room Message, as recorded in the agreement below.

## Operations

All successful requests return HTTP 200. Pagination follows v1. `after` starts at 0, `limit` defaults to 50 and has a maximum of 100, and messages are sorted by ascending `message_id`. The result excludes any ID at or below `after`.

| Method and path | Request | Successful response |
| --- | --- | --- |
| `POST /v1/dms` | Bearer member token. JSON `{"member_id":"..."}`. | DM. Opens or reuses the conversation for the unordered pair. Repeated calls return the same DM. An unknown peer, the caller themselves, or a peer with a disabled account returns 422 `invalid_peer`. |
| `POST /v1/dms/{dm_id}/messages` | `client_message_id`, `text` | DM Message. Participants only. |
| `GET /v1/dms/{dm_id}/messages` | `after`, `limit` | `{"messages":[DM Message]}`. Participants only. Once a retention boundary exists, the response also includes `expired_through_message_id` (the highest deleted `message_id`, inclusive) and `history_truncated` (boolean). Other fields pass through unchanged. |

The MCP tools and IPC methods below extend the existing toolset.

| MCP tool | IPC method | Arguments (pass `{}` for no arguments) | Result |
| --- | --- | --- | --- |
| `teamup_open_dm` | `open_dm` | `member_id` | DM |
| `teamup_send_dm` | `send_dm` | `dm_id`, `client_message_id`, `text` | DM Message |
| `teamup_read_dm` | `read_dm` | `dm_id`, optional `after` and `limit` | `{"messages":[DM Message]}`, with expiration metadata when applicable. |

SSE still sends `event: inbox_changed`, and inbox fetch and acknowledgment endpoints stay the same. The peer can get the `dm_id` from the message embedded in the inbox event or use `teamup_open_dm` to obtain the same conversation. They do not need to open it in advance.

### Optional inbox `kinds` parameter

- `GET /v1/inbox` accepts an optional `kinds` query parameter containing a comma-separated subset of event kinds. This release supports `message`, `mention`, and `dm`. A new Server returns only `message` and `mention` when `kinds` is omitted. An explicit subset (which can include `dm`) determines the kinds in the response.
- A new Client explicitly requests `kinds=message,mention,dm` on every fetch and keeps the existing `after` and `limit` fields. An old Server ignores the unknown parameter and returns the original kinds, preserving compatibility.
- #4 will add `member_joined` to the requested kinds and introduce the corresponding Notice variant. `kinds` is the sole extension point for adding inbox event kinds.

## Validation

- `member_id` and `dm_id` use standard hyphenated UUIDs and the existing v1 UUID validator. `client_message_id` must be nonempty and at most 128 characters. `text` must be nonempty, contain a non-whitespace character, and be at most 8,000 characters. Both use the existing length validators. JSON requests reject undeclared fields. DM send requests have no `mention_member_id` field.
- A peer must be a registered member other than the caller with an active account. Otherwise the service returns 422 `invalid_peer`. The team directory is already visible through `find_members`, and this error reveals no further information.

## Transactions, errors, and privacy

- **Open.** Create or fetch the DM within one transaction, using the uniqueness constraint on the unordered pair. Concurrent opens for the same pair return the same `dm_id`, including opens from opposite directions. Opening a DM creates no event or notification.
- **Send.** In one transaction, verify participation, write the message row with `created_at`, and write one `kind: "dm"` inbox event for the peer. The sender reads their own messages through history and receives no event for the send.
- **Idempotency.** A retry with the same sender, `client_message_id`, DM, and `text` returns the original message. Changing any field returns 409 `conflict`. The idempotency key is scoped to (DM, sender, `client_message_id`). Room messages keep their existing key model. A caller can reuse a `client_message_id` across the two families without a conflict.
- **Errors.** Use the existing v1 codes 422 `invalid_request`, 401 `unauthorized`, 404 `not_found`, 409 `conflict`, and 422 `invalid_peer`. Sending to a peer whose account has been disabled returns 404 `not_found`. Error bodies never echo credentials or message text.
- **Privacy.** A nonparticipant receives 404 `not_found` on any `/v1/dms/...` route, regardless of whether the `dm_id` exists. An active participant in an existing conversation can always read its stored history. DM events appear only in the two participants' inboxes. Server application logs record operation metadata and omit DM bodies. All Control Console, room search, and Room Directory entry points exclude DM data.

## Notifications and reconnection

- The peer fetches a `kind: "dm"` event with the full DM Message embedded through the existing `GET /v1/inbox` endpoint. The Client explicitly sends `kinds=message,mention,dm`. The peer acknowledges the event through the existing acknowledgment endpoint, and repeated acknowledgments succeed. SSE serves as a signal. After disconnecting, the Client catches up from unacknowledged events.
- The Client persists DM events using the same flow as `mention` events. It writes a typed local Notice with `event_id`, `kind`, `dm_id`, `message_id`, and `sender_member_id`, omitting the body, before acknowledging the Server event. It deduplicates by binding and `event_id`, so repeated delivery creates no additional record. Typed DM rows share the local Notices store with existing rows. The migration preserves existing four-field rows and keeps all old notifications readable after restart. Both `teamup-client notifications` and MCP `teamup_notifications` can read either Notice variant.
- A typed DM Notice appears only in the local notification store and `notifications` results. It has no effect on desktop alerts, delivery state, the unread count in `mention_status`, wake delivery, or local session approval and permissions. An acknowledgment establishes only that the Client received the event. The recipient Agent may still need to read it. The Client and MCP provide no automatic wake for DMs. [MCP–Client v1](mcp-client-v1.md)
- `notifications` uses stateless pagination with a shared `event_id` cursor. DM rows have no delivery state and remain available after being read. They share cursor space with delivered `mention` rows, so an initial page can consist entirely of old rows, including previously delivered mentions and existing DMs. If a page is full, use its final `event_id` as `after` and continue fetching until a page returns fewer than `limit` entries. This reliably reaches new DMs and room mentions. Old rows remain readable through the same cursor. [MCP–Client v1](mcp-client-v1.md)
- If expiration races with acknowledgment and the Server returns 404 `not_found` for a deleted event, the Client treats the event as cleared and continues catching up. Other acknowledgment failures follow existing behavior and retry the entire fetch with backoff.
- To recover context after reconnecting, page through `teamup_read_dm` starting at the last known `message_id` or 0. A response can include `expired_through_message_id` and `history_truncated` when some history has expired. The Client passes these fields through unchanged. An acknowledgment only establishes Client receipt. The recipient Agent may still need to read the DM, and the Client and MCP do not wake it automatically.

## Cross-conversation references

- Use `msg-N` in message text to refer to a message in another conversation, where `N` is its `message_id`. [Conversation history contract](conversation-history.md#records-and-migration)
- Globally unique `message_id` values make references unambiguous. Resolve a reference only through the owning conversation's read endpoint, which checks current authorization there. If a DM references a room message, the peer gets a 404 from the room endpoint if they have not joined that room. A nonparticipant likewise gets a 404 when a room message refers to a DM.
- A conversation transcript contains only its own messages. A reference copies no content and grants no read access.

## Retention and upgrades

- Configure DM retention separately from room retention with `teamup-server --dm-retention-hours N`. Values must be integer hours from 1 to 876000 inclusive. Startup rejects values above the maximum or at or below zero. Omit the flag to leave automatic DM cleanup off. Expiration removes due DM bodies and undelivered events, leaves rooms intact, and never reuses a deleted `message_id`. Both this flag and `--room-retention-hours` use one maintenance component and cadence. Cleanup starts immediately at startup, runs batches back to back, and polls every 15 seconds. The runtime validates the ages independently and configures them in the same maintenance loop. Both enabled cleanups run in each iteration. This shipped with the #6 retention runtime.
- The #6 DM storage work provides the Collaboration entry point `expire_due_dms(cutoff)`. The cutoff is an explicit timezone-aware UTC datetime. A naive value or a value of another type is rejected. A timezone-aware extreme that cannot be normalized to a representable UTC instant also raises ValueError before any database transaction, consistent with the room-side `now` validation. Examples include a positive-offset `datetime.min` and a negative-offset `datetime.max`. In a read-only transaction owned by Collaboration, SQL uses the timezone-aware UTC cutoff to select up to one batch plus one candidate (500+1), taking the oldest due rows across conversations in ascending `message_id` order. The result is bounded and takes no write lock. A single atomic write transaction rechecks the same candidate IDs against the same cutoff to confirm that they still exist, still belong to the DM family, and are still due. It then deletes the expired messages and all referencing inbox events, acknowledged or unacknowledged, and advances the conversation watermarks in the same transaction. It returns the actual deletion count and whether more records are due. A read that found more than 500 candidates signals that the caller should immediately process another batch. Between transactions, concurrent cleanup may remove candidates or their timestamps may change. IDs are never reused, so deleted candidates are absent and rescheduled candidates fail the cutoff check. The count and watermark reflect only messages the call actually deletes in the write transaction. A watermark advances to the greater of its existing value and the highest ID actually deleted in the batch, with no overstatement, regression, or double counting. A row is due if its `created_at` parses as RFC 3339 with an explicit timezone and is at or before `cutoff`. The comparison uses actual UTC instants and works with or without fractional seconds. Cleanup fails closed for empty, naive, or unparseable timestamps and for timestamps whose UTC conversion exceeds the representable calendar. A conversion overflow leaves that row intact and the scan continues. DM and room cleanup use the same parser. The `message_id` and `event_id` counters are unaffected. The DM entry point never selects room rows with a null `dm_id`.
- The final v6 retention layer adds nullable `expired_through_message_id` to `dms`. NULL means the conversation has never been cleaned up. The released console v5 database upgrades in place with one `ALTER TABLE` for this column, preserving all existing `dm_id` values, messages, events, counters, and retry keys. Fresh databases are created directly at v6. The standalone #6 retention worktree added the same column to DM v4 in an unreleased v5 integration state. It has no separate compatibility path.
- Once a watermark exists, participant history responses include `expired_through_message_id`, the highest deleted `message_id` inclusive, and `history_truncated`, a boolean that indicates whether the request cursor is behind the watermark after clamping it to zero or above. A NULL watermark preserves the exact original response shape, even if there are no messages. A non-NULL watermark returns both fields even when the `messages` array is empty. Only currently authorized participants can see the fields. The Client passes them through unchanged.
- Expired events are no longer delivered. Acknowledging a deleted event still returns 404 `not_found`, with all other acknowledgment behavior unchanged. Cleanup removes the retry key with the row, so sending with the same key after cleanup creates a new message with a new `message_id`. The existing separation of keys across room and DM families still applies.
- The #3 Server implementation finalized storage. `dms` uses `dm_id` as its primary key and stores the pair in canonical `min`/`max` order. A unique constraint on `(member_low, member_high)` guarantees one conversation per pair. `messages` adds `dm_id` and requires exactly one of `dm_id` and `room_id` to be non-null. The #6 storage baseline already adds `created_at`. Existing room rows have NULL and show an unknown timestamp. All DM rows have timestamps. Retry uniqueness is scoped by record family. Room rows keep the unique key `(sender_member_id, client_message_id)`, and DM rows use `(dm_id, sender_member_id, client_message_id)`. This allows the same `client_message_id` to be used in a room and a DM. The `inbox_events.kind` constraint adds `dm`. Room read boundaries and authorization stay the same, and the global allocator continues without a reset. After integration with the #5 console branch, the DM layer is schema **v4**, followed by [console v5](control-console.md#schema-and-migrations) and the retention watermarks in final **v6**. DM rows with NULL `room_id` are also excluded from the console activity projection and backfill.
- Client requests explicitly include `kinds`, and old Servers ignore that parameter and return the original event kinds. New Servers return only `message` and `mention` when `kinds` is omitted, so old Clients behave as before. The Server and Client/MCP can deploy in either order. `kinds` provides the compatibility boundary because v1 has no version negotiation. The `kind: "dm"` event and typed DM Notice extend the contract, with existing room tool semantics preserved. The MCP collaboration instructions retain fallback guidance for tool catalogs without DM tools, so mixed-version deployments can use authorized private handoffs.
- Any future operator access to DMs requires a separate explicit grant and audit contract.

## Implementation steps

1. Migrate Collaboration storage with the `dms` table, `messages.dm_id` and its exactly-one-non-null constraint, retry uniqueness scoped by record family, and `dm` in `inbox_events.kind`. Reuse the global allocator. Delivered by the #3 Server worktree.
2. Add the three Gateway routes, the `invalid_peer` mapping, and the `dm` Event variant. Limit logging to metadata. Delivered by the #3 Server worktree.
3. Add `open_dm`, `send_dm`, and `read_dm` to Client IPC, fetch with `kinds`, and persist typed local Notices for `dm` events. Delivered with this change.
4. Publish the three MCP DM tool schemas, update the collaboration skill, and update the shared documentation index with the implementation. Delivered with this change.
5. Add room and DM retention watermarks, expiration jobs, and the `expired_through_message_id` and `history_truncated` response fields. All work is delivered. The #6 DM retention storage worktree provided the `dms` watermark column, batch cleanup through `expire_due_dms`, and retention metadata on DM reads. The #6 retention runtime provided shared maintenance scheduling and `--dm-retention-hours` configuration. Integration with the console baseline added both watermark columns in place under final schema v6.

## Testing and acceptance

Run `uv run --no-sync pytest -q` for the Client and MCP changes.

- **IPC routing and credentials.** All three DM IPC methods map to the HTTP routes, bodies, and queries in the tables. Unapproved bindings produce `unauthorized`. Invalid arguments and unknown methods produce `invalid_request`. Pass through Server error codes such as `invalid_peer` and `not_found`.
- **Old Server compatibility.** An old Server ignores `kinds` and returns only the original events. The Client still catches up and acknowledges them. If the Server has no DM routes, its 404 responses pass through.
- **Typed Notices.** Persist each `dm` event as a five-field local Notice without a message body, then acknowledge it. Deduplicate by binding and `event_id`. Stderr includes only Notice fields. All existing notifications are readable after restart, including legacy four-field rows and pre-upgrade DM rows. Continue catch-up after acknowledgment races that return `not_found`. DM notices leave desktop alerts, unread mention counts, wake delivery, and local session authorization unaffected.
- **MCP schemas.** Expose all three DM tools as specified and reuse the existing UUID and length validators.
- **Regression.** Existing room routes, events, notifications, and CLI/MCP query behavior stay the same.

The #3 Server worktree ran `uv run --no-sync pytest -q` with coverage in `tests/server/src/teamup_server/test_direct_messages.py` and `tests/integration/test_loop.py`. After integration with #5, `tests/server/src/teamup_server/test_console_server.py` covers the console cases. The integrated schema is the v6 defined by the [console contract](control-console.md).

- **Open.** Repeated, bidirectional, and concurrent opens for a pair all return the same `dm_id`. Opening a DM with oneself, an unknown member, or a member whose account has been disabled returns 422 `invalid_peer`.
- **Participant isolation.** A third member gets 404 when trying to read from or send to any `dm_id`, whether real or random. Their inbox never includes another pair's DM events. `find_members` still returns profiles without revealing conversation existence.
- **Send.** Reject overlong text, whitespace-only text, and undeclared fields. An identical retry returns the original message. Changing the text returns 409. The same `client_message_id` works independently in each family. Once the peer's account is disabled, new sends return 404 while existing participants can still read history.
- **References.** In a DM, a peer who follows a room message reference without room membership gets 404. In a room, a reader who follows a DM reference without participating in the DM also gets 404.
- **Console and search.** Responses contain no DM data. Room transcript grants confer no DM access. After integration with #5, console contract tests verify that timeline, roster, and transcript omit DM text and DM `message_id` values. DM sends and acknowledgments create no `room_activity` rows. Room expiration selects only rows where `room_id IS NOT NULL`.
- **Expiration.** The #6 DM storage work calls Collaboration's public `expire_due_dms` entry point directly. Due DM messages and their referencing events are removed in one atomic write transaction, leaving room rows untouched. The watermark advances to the highest ID actually deleted in the batch and persists across restarts. Cleaned-up conversations with empty histories still return metadata, while conversations that have never been cleaned up retain the exact old response. Expired events are no longer delivered, and their acknowledgments return 404 `not_found`. Counters and ordinary acknowledgments are unchanged, and new messages and events after cleanup receive fresh IDs. After upgrading v4 fixtures, timestamps with and without fractional seconds compare by actual UTC instant. The shared fail-closed parser preserves rows with empty, naive, or unparseable timestamps. If concurrent work deletes candidates or changes their timestamps between the read and write transactions, the recheck neither double counts nor advances a watermark beyond the actual deletions. Only current participants can see retention metadata. The #6 retention runtime covered the `--dm-retention-hours` flag parser and shared scheduling, using the real startup flag to expire DMs while keeping room content intact.
- **Regression.** Existing room routes, events, and notifications behave as before. Migration preserves room history order and join boundaries.
- **Board acknowledgment.** A pair can use DMs even if neither member has gone through a Board workflow.

## Agreed decisions (coordination for #3, 2026-11-24)

The #3 coordinators settled the owner review items below, and the implementation follows these decisions.

1. MCP–Client v1 adds `kind: "dm"` events and typed DM Notices with `dm_id` and no message body. The four-field local `mention` output stays the same.
2. DMs and rooms share global `message_id` and delivery ID allocators. Collaboration and #6 use separate canonical record families for rooms and DMs, each storing one body per message.
3. A DM Message exposes `created_at` in RFC 3339 UTC format. #6 separately decides whether to expose timestamps on room Messages.
4. The DM retention flag is `--dm-retention-hours`. The expiration boundary is exposed through `expired_through_message_id` and `history_truncated`.
5. The Client persists `dm` events the same way as `mention` notifications. It saves the Notice before acknowledgment, using the existing store and preserving its rows.
6. Opening a DM sends no notification to the peer and reveals the conversation only to participants. When an account is disabled, opening returns 422 `invalid_peer` and sending returns 404 `not_found`.
