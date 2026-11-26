# TeamUp for macOS

TeamUp.app opens a single desktop window for the local Client console. It can use an existing profile and reuse a Client started by the TeamUp launcher. Sign in once and select **Remember me on this Mac** to use Keychain on future launches over HTTPS or a [private SSH forward](../../../docs/modules/team-pilot.md#开发者连接与安装). **TeamUp → Forget Saved Credentials** removes the saved sign-in. A signed-in console enables wake for approved sessions by default. Change it in Workspace.

## Build and open

On macOS 13 or newer, install Node.js 22.12 or newer, Python 3, and Xcode Command Line Tools. Run these commands from the repository root.

```sh
npm --prefix client/macos/desktop ci
npm --prefix client/macos/desktop test
npm --prefix client/macos/desktop run package:mac
```

The build uses the same version source as the Client package. Build both from the same tagged Git history or the same stamped source export.

The build prints the path to `TeamUp.app`, signed ad hoc for local installation. Copy the bundle to your Applications folder and open it from Finder or the Dock. It includes Electron and the console UI. Startup uses `~/.config/teamup/client.json` and the `python` and `teamup-client` executables in `~/.local/share/teamup/clients/current/.venv/bin`. See [Client setup](../../../docs/modules/quickstart.md#启动-client) for the private profile format.

For development, build the Keychain helper and use the workspace's environment with a private test profile.

```sh
uv sync --locked --all-packages
npm --prefix client/macos/desktop run build:keychain
TEAMUP_DESKTOP_RUNTIME="$PWD/.venv/bin" \
TEAMUP_DESKTOP_CONFIG="/absolute/path/to/client.json" \
npm --prefix client/macos/desktop start
```

The development app honors `TEAMUP_DESKTOP_STATE_DIR` for a separate launcher state directory. The packaged app uses the current OS user’s standard profile, installed runtime, and state directory. Startup logs are stored in a private profile directory under `~/Library/Application Support/TeamUp Launcher` by default.

## Verify

Open **TeamUp → About TeamUp** and verify the full build version, including `.devN` on a development build. When the Client was installed from the same build, the **Client** footer should show the same version.

Open TeamUp.app twice and confirm the existing window comes forward. In a profile with approved sessions, open a joined room and check its transcript and Tasks tab. Close and reopen the window to verify it can reconnect to the same running Client. The [console contract](../../../docs/modules/client-console.md) describes the API and session authorization.
