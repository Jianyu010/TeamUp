# TeamUp account login v1

Agent self-retirement, owner-managed departure, and retired history are specified in [Session retirement and room departure](member-lifecycle.md).

The first release targets a pilot on a shared host within a trusted team. Each developer has a TeamUp account and can link several agents to it. TeamUp accounts are separate from SSH system accounts. In a test environment where a shared SSH account can obtain root access, everyone who uses the server must be trusted. Collaboration owns writes to accounts, password hashes, and login sessions, as well as member deactivation.

## Accounts and administration

- Usernames are 3–64 lowercase ASCII characters. They start with a letter and may contain digits, underscores, and hyphens. Each account has an immutable UUID as its `owner_id`.
- On the server, administrators run `teamup-server accounts --db PATH create USERNAME` or `reset-password USERNAME` as the service account. Both commands prompt twice for the password without echoing it. Use `list` to check account status and `disable USERNAME` to disable an account. The commands use Collaboration's public entry points and the same application database. Account creation and deactivation are available only through the CLI.
- Passwords must be 12–128 characters. They are accepted as entered and stored as Argon2id hashes. Resetting a password invalidates all human login sessions for that developer. Credentials already granted to agents remain valid. Permanently disabling an account invalidates both human logins and credentials for its agents. Member IDs remain in historical records. Account deactivation is idempotent. Disabled accounts are never reused.

## Client–Server additions

| Method and path | Input | Result |
| --- | --- | --- |
| `POST /v1/auth/login` | `{"username":"alice","password":"…","purpose":"console"}`. The optional `purpose` defaults to enrollment when omitted or `null`, preserving existing behavior. The only accepted non-null value is `"console"`. Other values return 422 `invalid_request`. | HTTP 200, `{"access_token":"…","expires_at":1780000000}`. The expiration is in Unix seconds. Tokens last eight hours. The server generates each token from 32 random bytes and persists its digest. |
| `POST /v1/members` | Submit the binding defined in the existing contract. A valid enrollment-purpose human login token can authorize the request as the Bearer credential. | The `owner_id` of the valid login session determines ownership. The [original contract](client-server-v1.md) defines the rest of the request, response, and idempotency rules. |

A human login token issued for enrollment can register agents, rename members owned by the same account, or retire them. A member token authorizes member APIs only. Human login tokens, member tokens, and legacy enrollment keys each have their own scope. The server keeps the type record for issued human token digests after expiration or revocation. Legacy `TEAMUP_ENROLLMENT_KEYS` continue to authorize enrollment on v0 instances that explicitly configure them. Omitting the variable leaves legacy enrollment keys unavailable. Existing SQLite databases upgrade automatically, and v0 member identities and history remain readable. This pilot uses a new database and account-based login.

### Member profile management (#47)

| Method and path | Input | Result |
| --- | --- | --- |
| `PATCH /v1/members/{member_id}` | `{"role_description":"…"}`, validated against the enrollment rules for roles. | HTTP 200, Member. Updates the role and preserves the member ID, credentials, and room eligibility. Submitting the same role again returns the same profile. |
| `POST /v1/members/{member_id}/retire` | No body. | HTTP 200, `{"member_id":"…","retired":true}`. Only the first committed request changes member state. Repeated requests return the same result. |

Both routes require a valid enrollment-purpose account token. Member tokens, console tokens, legacy enrollment keys, and invalid or revoked account tokens receive 401 `unauthorized`. If the target is missing or belongs to another account, the response is 404 `not_found`. Renaming a retired member returns 409 `member_retired`. Collaboration checks the account and ownership within the write transaction. See [Client–Server v1](client-server-v1.md#member-retirement-and-room-departure-47) for authorization, history access, and retries after retirement and room departure.

### Login purpose (console sessions, shipped in #5 stage 2)

- The server stores the `purpose` on the session. Tokens issued for `"console"` can call only the read-only `/v1/console/*` routes published by the [Control Console](control-console.md). Sessions created without a purpose retain enrollment semantics. During migration, every existing session receives the enrollment purpose. Both purposes share the account lifecycle, login rate limits, password hash domain, and digest domain for 32-byte credentials. A new token digest must be distinct from every member token, legacy enrollment key, and other session digest.
- The scopes are enforced in both directions. A `console` token receives 401 `unauthorized` from `POST /v1/members` and 401 from ordinary member routes. Enrollment-purpose login tokens, member tokens, and legacy enrollment keys receive 401 from all `/v1/console/*` routes. Passing a `console` token as `member_token` to `POST /v1/members` returns 422 `invalid_request`.
- Account deactivation and password resets continue to revoke all current sessions for that account, including console sessions. Password resets preserve member credentials already granted to agents. Login responses, errors, and logs never echo passwords or tokens.
- Collaboration persists the session purpose. In schema v5, `account_sessions` has a `purpose` column and existing rows default to enrollment. The Control Console contract covers the `operator_grants` table. Collaboration migrates existing v2 databases, the #6 history v3 draft, #3 DM v4, and the published #5 authentication preview v3 (`e495544`) to v5 internally. The migration identifies the preview by its tables and columns, and preserves its existing purposes, sessions, and grants. No external migration tool is required.

Errors use the v1 JSON shape. Incorrect passwords, invalid accounts, and disabled accounts all return 401 `unauthorized`. Invalid JSON or fields return 422 `invalid_request`. Login allows at most five failures per username per minute and 60 password checks per instance per minute. Exceeding either limit returns 429 `rate_limited`. Each instance process tracks its own limits. Responses, errors, and logs never echo the submitted password.

Account deactivation and token checks use the same SQLite authorization state. Once deactivation commits, all new application requests from the account's members return 401. Open SSE connections recheck authorization before sending the next notification or keepalive. The keepalive interval is at most 15 seconds. Notifications already delivered to the Client remain available locally to the associated binding.

## Client startup configuration

Open TeamUp.app or run `teamup-client serve --config PATH --console` and open the console in a browser to enter your TeamUp username and password. The Client authenticates through the existing enrollment-purpose login API and saves the credential before starting local IPC. The desktop launcher reuses a running Client with the same configuration. See the [local console sign-in contract](client-console.md#sign-in-to-the-client).

`teamup-client login --config PATH --username USERNAME` prompts for the password in the local terminal without echoing it. The command uses the existing Server URL and TLS trust settings, then atomically writes `account_token` to the local configuration with mode 0600. The success output omits the token. The initial configuration can omit credentials. If login fails or the configuration file changes while the command is waiting, the command preserves the file and prompts the user to retry. Stop any Client using the same configuration before logging in and restart it after login succeeds. The Client configuration accepts either `account_token` or the legacy `enrollment_key`.

The MCP tools, IPC, agent binding fields, and member credential format continue to follow [MCP–Client v1](mcp-client-v1.md). Before enrollment, the Client checks for an existing identity with its member token and reuses it if the lookup succeeds. It uses a human login token only for a binding that has yet to enroll, so human login expiration and subsequent sign-ins leave existing agents running. The Server continues to reject agents belonging to disabled accounts.

## Pilot deployment

The remote Server listens on loopback. Team members use their existing SSH connections to forward it to their respective local loopback interfaces. SSH encrypts the forwarded connection. Each developer enters their TeamUp account credentials only in their own Client. Run the Server under a dedicated system account. To validate the pilot, have two people log in, register, chat, and exchange @mentions. Disable one account and verify that its existing member credentials can no longer fetch or send messages.

### Member responsibility for a room (#75)

`PATCH /v1/rooms/{room_id}/members/{member_id}/responsibility` takes
`{"membership_id":"…","description":"…"}`. The description may be null to clear the
override. Only the member's account owner can write it with an enrollment-purpose
account token. Success returns `{"description":"…"}` with the configured override or
null. See [Member names and responsibilities](member-profiles.md) for the input,
generation check, and inheritance rules.
