# TeamUp Admin Console

Gateway provides the browser routes. Collaboration owns accounts, operator grants, session checks, and credential updates. The Client Console discovers access through Gateway and opens the configured Server's console. The Server Console uses the existing, separate console-purpose sign-in.

## Access and navigation

- The Client checks `GET /v1/auth/capabilities` with the current enrollment-purpose account token. The response is `{"admin":true}` when an active account has `console_read`, and `{"admin":false}` otherwise. Expired, revoked, member, legacy enrollment, and console credentials return 401.
- The local Client exposes the configured Server's `/console/` URL only after a positive check. It omits the Admin entry if the Server is unreachable, runs an older build, or denies access. Nothing from the enrollment session is sent to the browser. macOS uses one **View → Admin** entry and the same destination as the button beside **Live**. The desktop checks access again before opening the configured URL.
- The Server console displays a TeamUp sign-in first. The user signs in with their TeamUp username and password. `GET /v1/console/capabilities` requires a valid console session and `console_read` and returns `{"maintenance":boolean,"accounts":boolean}`. Rooms and the public CI dashboard are available to console operators. Server Dashboard appears for operators with `maintenance_read` or `logs_read`. Health and room load require `maintenance_read`. Redacted events require `logs_read`. Room transcripts retain the existing per-room grant. Existing `/console/` and `/dashboard/` links continue to work.
- The client-side capability is for presentation. Collaboration checks the correct session class, expiry, account status, and relevant grants on each protected Server request. Successful capability and admin responses use `Cache-Control: no-store`. The CI dashboard is embedded from its fixed GitHub Pages origin and receives no TeamUp credentials.

## Accounts

A persistent account-management grant belongs to an account's immutable owner ID. Server-local operators use `teamup-server accounts --db PATH grant-account-admin USERNAME` and `revoke-account-admin USERNAME`. These commands are idempotent and independent of existing grants. An account needs both `console_read` and the account-management grant for the Accounts tab. Credential resets, restarts, schema upgrades, and deployments preserve the grant. No username is assigned this grant by application code.

| Method and path | Input | Success |
| --- | --- | --- |
| `GET /v1/console/accounts` | None | 200 `{"accounts":[{"username":string,"disabled":boolean,"can_reset_password":boolean,"can_disable":boolean}]}`, sorted by username |
| `POST /v1/console/accounts` | `{"username":string,"password":string}` | 201 `{"username":string}` |
| `POST /v1/console/accounts/{username}/password` | `{"password":string}` | 200 `{"username":string}` |
| `POST /v1/console/accounts/{username}/disable` | None | 200 `{"username":string}` |

These routes require a console-purpose session, `console_read`, and the account-management grant. Create makes a standard account. Web administrators can reset passwords and disable standard accounts. Accounts with any operator grant are protected from credential resets and disablement through the web interface, including the signed-in operator. Local CLI administration retains its existing authority. The same owner transaction checks the session, actor grants, target grants, and account state and applies the write. Local CLI and Gateway use the same owner mutation methods. The account list never includes passwords, token values or digests, or owner IDs.

Username and password rules and session revocation follow [account login](account-login-v1.md). Reset revokes all of the target's human sessions and preserves Agent credentials. Disable is idempotent and revokes human sessions and Agent access. Duplicate creation or resetting a disabled account returns 409, missing targets return 404, invalid input returns 422, and a valid console credential with insufficient authority returns 403. Absent, wrong-purpose, expired, or revoked credentials return 401.

The deployment owner explicitly grants account management to the already-authorized pilot operator. Verify standard accounts have no Client entry and cannot read or write account data. Verify the administrator can create a standard account, reset its password, and disable it. Check that operator grants and pilot data survive an upgrade.

The supplied [HTTPS proxy](team-https.md) forwards the exact console pages and assets alongside the API. The browser must trust the configured Server certificate.
