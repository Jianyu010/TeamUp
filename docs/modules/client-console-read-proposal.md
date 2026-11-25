# Client console read data (#34)

**Design proposal — not a published contract.** Consumer: the local read-only console served by one machine's TeamUp Client over its approved configured sessions ([#34](https://github.com/Jianyu010/TeamUp/issues/34)). Providers: Server Gateway read ports over Collaboration-owned state, plus Presence for [#31 scoped presence](https://github.com/Jianyu010/TeamUp/issues/31). Related: [#3 DMs](https://github.com/Jianyu010/TeamUp/issues/3), [#4 room directory](https://github.com/Jianyu010/TeamUp/issues/4), [#5 control console](https://github.com/Jianyu010/TeamUp/issues/5), [#6 history and retention](https://github.com/Jianyu010/TeamUp/issues/6). Surveyed at base `94261a7` against code and the published contracts ([Client–Server v1](client-server-v1.md), [MCP–Client v1](mcp-client-v1.md), [conversation history](conversation-history.md), [collaboration surfaces](../specs/collaboration-surfaces.md)). Owner approvals in the table below are required before any code.

The console is a **viewer**: it renders rooms, boards, rosters, DMs and presence for the local operator. No composer, no join, no wake changes, and no server telemetry is added here. It reads with each approved session's own member credential; the operator credential is never reused across client memberships, and existing member `GET /v1/rooms` stays unchanged.

## What is live at the base commit

| Console need | Live path | Boundary |
| --- | --- | --- |
| Session inventory | Owner IPC `sessions`, `decide_session`, `set_wake` (client `_coordinator.py:94`) | Local only. The owner key grants session management, never member reads |
| Own profile | `GET /v1/members/me` | Member token; disabled account returns 401 |
| Member directory | `GET /v1/members?q=` | Role substring, at most 100 rows |
| Rooms per session | `GET /v1/rooms` | Membership-scoped summary with `acked_board_version`, `current_board_version`, `latest_message_id`; unchanged by this proposal |
| Room transcript | `GET /v1/rooms/{room_id}/messages` | Requires current board ack, current membership and the history-policy floor (`collaboration.py:291-305`); `after`/`before` exclusive bounds |
| Room search | `GET /v1/rooms/{room_id}/search` | Same authorization; bounded snippets only |

The entry the console **must not** use as a viewer:

- `GET /v1/rooms/{room_id}/board` has a receipt side effect: `read_board` runs in a write transaction and records `read_board_version` (`collaboration.py:744-756`). That read receipt preconditions `POST .../board/ack` (`board_read_required`), so a console render through it would forge the member's take-receipt and unlock an acknowledgment the member never performed. The Client's pre-wake recheck (`_coordinator.py:615`) is the member's own delivery flow, not a viewer path, and stays.

Gaps without a side-effect-free read today: board content, room roster with per-member ack state, room facts such as member count, a DM index, and presence.

## Proposed read ports

All are additive `GET`s under Bearer member tokens, read-only transactions, and per-request re-authorization. Errors reuse the published JSON shape and codes (401 `unauthorized`, 404 `not_found` for non-members, 422 `invalid_request`). Board acknowledgment is **not** required for these metadata reads — like `GET /v1/rooms` summaries they stay visible while an ack is pending; only transcript and search keep the ack gate.

### A. Room board, roster, directory and membership

| Route | Result | Notes |
| --- | --- | --- |
| `GET /v1/rooms/{room_id}/board/view` | Board: `room_id`, `version`, `rules`, `protocol` | Same fields as the receipt-bearing read; never writes `read_board_version`; the ack flow is untouched |
| `GET /v1/rooms/{room_id}/members` | `{"members":[RosterEntry]}` | Current room members only; repeat membership returns one row per member |
| `GET /v1/rooms/{room_id}` | `{"room_id","name","history_policy","current_board_version","member_count"}` | Room facts for a room the caller is in. `member_count` is proposed; joinable-room discovery uses the proposed directory below |

RosterEntry (**proposed**): `member_id`, `role_description`, and `acked_board_version`. The console compares each acknowledged version with the current board version. A recent-communication reference is optional and requires the viewing session's current transcript authorization and readable history boundary. Task titles and collaborators come from the session's authorized [room tasks](room-tasks-proposal.md). The member list for a session remains `GET /v1/rooms`, and the team directory remains `GET /v1/members`.

[#4](https://github.com/Jianyu010/TeamUp/issues/4) shipped the joined-room listing at this base. For discoverable rooms outside that listing, review an additive member-scoped directory with a bounded page of room IDs, names, short purposes, and a joinable flag. Return only rooms the caller may discover. Room details and history retain their access checks, and joining remains an explicit operation.

### B. Participant-scoped DM index (retention-aware)

`GET /v1/dms` returns `{"dms":[DMEntry]}` for conversations the calling member participates in. DM send and read stay in #3; this is the read-side index the console needs.

DMEntry (**proposed with the #3 owner**): `dm_id` (stable, allocated when the pair opens the conversation), `peer_member_id`, `retained_message_count`, `latest_message_id`, `latest_message_at` (null when the latest retained message predates timestamps), `expired_through_message_id` (the #6 retention boundary; present once cleanup has removed messages, null otherwise), and optional `origin`:

- `origin` = `{"room_id","message_id"}` referencing the room `@` that led to the DM (**proposed**; the #3 owner confirms whether and when it is recorded).
- If adopted, the explicit link is stored with the DM and returned only to the two participants. Room reads, room search and other members' views never show it; the room `@` itself gains no "taken to DM" indicator.
- It is a pointer, not a copy: resolving it goes through the ordinary transcript read with the caller's current authorization; an expired or inaccessible message renders as unavailable without leaking room content.
- The session's conversation view can list authorized room @ events alongside its direct messages using their existing room message identifiers. A room @ by itself creates no DM and does not populate `origin`.
- The orbit uses retained-message count and latest retained-message time. Keeping partner identities or all-time totals after message expiry requires a separate retention decision.

### C. Presence ownership and observation age (#31)

- **Ownership.** Presence owns heartbeats and harness observations as its own state; Collaboration remains the authorization owner through its authorization query port (architecture component graph). Presence is observation, never a business authorization basis — it never gates transcript, roster or DM reads.
- **Scope.** `GET /v1/presence` (**proposed**) returns one entry per member sharing at least one room with the caller; arbitrary member-ID probes are outside this scope. Team-wide presence requires a separate approved observation scope. Until then, the Team view can show Unknown for members outside the shared-room scope.
- **Fields.** PresenceEntry (**proposed with the #31 owner**): `member_id`, `state` in `online|offline|unknown`, `observed_at` (server receive time of the last heartbeat, RFC 3339, null when unknown), optional `harness_state` in `idle|busy|unavailable|unknown` with `harness_observed_at` when a supported Driver reports it. No hostname, task content or local paths (MVP health-probe privacy).
- **Age.** State is computed at read time from server-received timestamps: fresh heartbeat is online, explicit disconnect or expiry is offline, no observation is unknown. The constants (15 s heartbeat, 45 s expiry in the MVP) remain a #31 decision. Reads are side-effect-free and add no telemetry.

## Compatibility

- New routes only; published routes, fields and error codes are unchanged, and old clients can ignore the new ports and fields.
- `board_read_required` semantics are unchanged: only the receipt-bearing board read satisfies it; viewer renders never do.
- Retention metadata follows the #6 shapes (`expired_through_message_id`, `history_truncated`); the DM index reuses the same boundary fields.

## Permission revocation

- Every request re-authenticates the token (including the account-disable check) and re-checks membership, DM participation and presence scope. Room removal and account disabling apply to the next request. A DM access change follows the participant contract.
- Issued pages and cursors confer no access: after revocation the same cursor returns 401/404 instead of data, matching the #6 acceptance for old cursors.
- The console's local rendering cache is a projection only, never an authorization basis, and must drop a view on the first 401/404.

## Cursor semantics

- Transcript keeps the published `after`/`before` exclusive bounds; search keeps `after`/`limit`; `limit` defaults to 50 and max 100, ascending by stable message ID.
- The DM index pages with `after` on `dm_id` (exclusive lower bound) and the same `limit` rules, ascending. New DMs whose IDs sort before a cursor appear on refresh from the beginning.
- Roster, room detail and presence are bounded whole views without pagination in this slice (team-sized rooms); adding cursors is an owner decision if rooms grow.
- Cursors are positions, not grants: every page re-runs authorization and retention filtering at read time.

## Credentials

- The console reads through each approved session's binding. The owner key stays limited to `sessions`/`decide_session`/`set_wake`, and the account token cannot call member APIs (server-side `_member` accepts member token digests only), so no operator credential can fetch another membership's data.
- A session that never joined a room sees nothing of it, even on the same machine. No operator-wide room list is introduced; #5's server-side operator console with transcript grants and audit is a separate scope.

## Owner approvals needed

| # | Decision | Owner |
| --- | --- | --- |
| 1 | Room read and discovery routes; RosterEntry fields; room detail `member_count` and `history_policy` exposure | Collaboration + Gateway |
| 2 | `dm_id` allocation; DMEntry fields; the `origin` recording point and privacy rules; DM retention boundary exposure | DM (#3) owner + Collaboration |
| 3 | `GET /v1/presence` scope; PresenceEntry fields; heartbeat/expiry constants; harness observation reporting | Presence (#31) owner |
| 4 | Console-side binding use for reads; local cache rules; any IPC surface extension | Client owner |

Every field marked **proposed** above requires its row's approval before implementation.

## Acceptance tests

1. **Board view is side-effect-free.** It returns the same fields as the receipt-bearing read; a member who never called `GET .../board` still gets 409 `board_read_required` on `POST .../board/ack` after console renders.
2. **Roster and room detail.** A member sees the roster with per-member ack state while its own ack is pending; a non-member, a removed member and a disabled account get 404/401.
3. **DM privacy.** Each participant sees the shared entry with the retention boundary after expiry; a third member gets no entry; `origin` resolves only while the reader can still read that room message; room history and search never surface `origin` or any DM fact.
4. **Presence.** `online` with a fresh heartbeat, `offline` after expiry or disconnect, `unknown` without observation; `observed_at` is the server receive time; no entries for members without a shared room; the read writes nothing and emits no telemetry.
5. **Cursors and revocation.** `after`/`before` behave as published on the transcript; the DM `after` pages without gaps or duplicates; after revocation the same cursor and a direct read both fail closed.
6. **Credentials.** With only the owner key, no member-data read succeeds; with one session's binding, another local session's rooms and DMs are invisible on the same machine.
