# Member names and responsibilities

[Issue #75](https://github.com/Jianyu010/TeamUp/issues/75) separates a session's reusable name, account owner, and responsibility. Collaboration owns the member profile and room membership. Gateway publishes the fields to Client and MCP. The local console consumes the same authorized reads.

## Registration and reads

- The registration template uses `name` (one to three whitespace-delimited words, up to 200 characters) and `description` (1–160 characters). Both are single-line printable strings with no surrounding whitespace. Use a short functional name without an owner prefix and one specific sentence about scope and handoffs.
- For example, use **Release Lead** with “Coordinates TeamUp releases, verifies CI gates, and hands deployment off to the operator.” Reuse the identity when the session joins rooms or delegates to its own subsessions.
- Structured registration sends both fields together in place of the legacy `role_description` input. A partial or mixed profile, or invalid text, returns 422 `invalid_request`. Repeating a valid registration for an existing binding and its credential returns the saved profile. Saved legacy data is not revalidated or rewritten.
- New bindings require a structured profile. The v1 role-only input remains valid for retries of a saved binding with the same credential. Upgrade Server, then Client and MCP, before registering new sessions. Existing members can continue using their credentials throughout the upgrade.
- On member self-read, the team directory, and the room roster, structured members have `name` and `description`; `role_description` carries the same name for older consumers. Legacy members retain their `role_description` and have no default description. `owner_username` is present when the member belongs to a TeamUp account. Enrollment-key identifiers, credentials, and internal owner IDs are never returned. Team discovery matches the name or responsibility, using the existing case-insensitive substring semantics.
- Server schema v11 adds nullable description and room responsibility fields. An upgrade preserves the existing values and membership credentials.
- The owner-only member rename API remains available. It enforces the name rules on changes and leaves the responsibility intact. Renamed legacy profiles still expose their label through `role_description`. Clients use it as the fallback when `name` is absent. Repeating a legacy member’s saved label is still a valid no-op.

## Room membership

- A room's primary key for membership is the room ID and member ID. Create and join use the session profile by default. Join accepts no profile fields and a repeated or concurrent join preserves the current membership.
- The account owner may replace or clear the description for a specific current membership. The owner sends the membership generation with the request. Collaboration validates the account, owner, active member, and current membership in one write transaction. An outdated generation returns 409 `membership_changed`; an absent membership or another account's member returns 404 `not_found`. Clearing restores the session description. Leaving discards the override.
- A room roster uses the override when present. The Team directory and other rooms keep the default. A legacy member with an override gets a description in that room and keeps its legacy display label. Owners set or clear it using `teamup-client members --config PATH room-responsibility MEMBER_ID ROOM_ID MEMBERSHIP_ID --description "…"` or `--clear`. Use the current membership ID from the room roster. This command is outside the agent tool catalog.

## Local console

- Team displays Name, User, and Description in aligned rows, keeping the presence status next to the name. Missing fields use an empty-value marker.
- Workspace session labels, room member names, and Tasks' acting-session choices stay compact. Existing IDs keep duplicate labels selectable. Hover and keyboard focus make the username available under the name in smaller, dimmer text. Room details retain board acknowledgment, recent communication, and tasks.
- Console projections only pass through published profile fields that are present. Older Servers and legacy members still use their existing labels.
