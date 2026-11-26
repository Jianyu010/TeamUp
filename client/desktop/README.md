# TeamUp for macOS

For Windows, see [the Windows launcher over MCP](windows/README.md).

TeamUp.app opens a single desktop window for the local Client console. It can use an existing profile and reuse a Client started by the previous TeamUp launcher.

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
