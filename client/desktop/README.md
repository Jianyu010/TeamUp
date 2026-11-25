# TeamUp for macOS

TeamUp.app opens the local Client Console on launch. It can use an existing profile and reuse a Client started by the previous TeamUp launcher.

Choose **View → Server Dashboard** to open the configured Server's maintenance console at `/dashboard/`. **View → Room Console** opens the read-only room interface at `/console/`, including on older Servers that only publish the Room Console. Sign in with a TeamUp account that has `console_read`. Health and room load require `maintenance_read`, and redacted events require `logs_read`. For the pilot profile, keep its SSH tunnel running. An HTTPS origin must use a certificate trusted by macOS.

Choose **View → CI Dashboards** to open the public dashboards in a separate window. Browse the overview, CI, repository, performance and Agent pages without signing in. [Published CI Dashboards](https://jianyu010.github.io/TeamUp/)

## Build and open

On macOS 13 or newer with Node.js 22.12 or newer, run these commands from the repository root.

```sh
npm --prefix client/desktop ci
npm --prefix client/desktop test
npm --prefix client/desktop run package:mac
```

The build prints the path to `TeamUp.app`, signed ad hoc for local installation. Copy the bundle to your Applications folder and open it from Finder or the Dock. It includes Electron and the console UI. Startup uses `~/.config/teamup/client.json` and the `python` and `teamup-client` executables in `~/.local/share/teamup/clients/current/.venv/bin`. See [Client setup](../../docs/modules/quickstart.md#启动-client) for the private profile format.

For development, use the workspace's environment and a private test profile.

```sh
uv sync --locked --all-packages
TEAMUP_DESKTOP_RUNTIME="$PWD/.venv/bin" \
TEAMUP_DESKTOP_CONFIG="/absolute/path/to/client.json" \
npm --prefix client/desktop start
```

`TEAMUP_DESKTOP_STATE_DIR` can select a separate launcher state directory for testing. Startup logs are stored in a private profile directory under `~/Library/Application Support/TeamUp Launcher` by default.

## Verify

Open TeamUp.app twice and confirm the existing window comes forward. In a profile with approved sessions, open a joined room and check its transcript and Tasks tab. Close and reopen the window to verify it can reconnect to the same running Client. The [console contract](../../docs/modules/client-console.md) describes the API and session authorization.

Open the Room Console twice and confirm the existing operator window comes forward. Sign in, confirm the login card disappears, and open a room's Members and Timeline. Confirm that Chats follows the Server's room grant. Open the Server Dashboard, sign in separately, and check health or redacted events with the corresponding grant. Close and reopen an operator window to verify it starts with a fresh sign-in.

Open CI Dashboards and follow the links among the published pages. Confirm the app shows the overview when opening the window and brings the existing CI window forward on a second click.
