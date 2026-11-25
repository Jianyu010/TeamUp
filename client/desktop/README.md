# TeamUp desktop

TeamUp.app opens a single desktop window for the local Client console. It can use an existing profile and reuse a Client started by the previous TeamUp launcher.

## Windows x64 (WSL2 backend)

The Windows package opens `TeamUp.exe` as a native Electron window. Its Python Client stays in WSL2 and uses the existing private profile and Server connection. It is not a native Windows Python Client.

Prerequisites: Windows with WSL2, Python 3 available as `python3` in the selected distribution, an installed TeamUp Client supporting `serve --console`, and a configured private profile. The default WSL profile is `~/.config/teamup/client.json`. Install the runtime using [Client setup](../../docs/modules/quickstart.md#启动-client), then point `~/.local/share/teamup/clients/current` at that checkout (whose `.venv/bin` contains the Client).

On Windows, with Node.js 22.12 or newer:

```powershell
npm --prefix client/desktop ci
npm --prefix client/desktop test
npm --prefix client/desktop run package:win
```

The output is `client/desktop/dist/TeamUp-win32-x64/TeamUp.exe`. Keep the whole directory when copying or zipping the app. The local preview is unsigned. Open it twice to bring the existing window forward. Closing the window leaves the Client receiving events.

To install or upgrade the desktop window for the current Windows user, extract the
portable package outside the installed app and run its bundled installer:

```powershell
& '.\TeamUp-win32-x64\Install-TeamUp.ps1' -Start
```

The installer stages the complete package, closes only TeamUp processes from the
destination, installs into `%LOCALAPPDATA%\Programs\TeamUp`, and creates or updates
the desktop shortcut. An existing installation is retained beside it as
`TeamUp.previous-<id>` for manual rollback. An incomplete package is rejected before
the installed app is changed. The installer does not request administrator rights
or change execution policy; a managed policy that prohibits scripts must be respected.

The installer updates the Windows app only. Upgrade the WSL Client runtime using
the Client setup instructions as well to use new console APIs. Retain the private
profile and Client state, point `clients/current` at the new runtime, and restart
the existing Client through its service or launcher. Installing new UI assets while
reusing an older running Client does not upgrade that Client.

Optional PowerShell settings select **WSL paths**, not Windows paths:

```powershell
$env:TEAMUP_DESKTOP_WSL_DISTRO = 'Ubuntu'
$env:TEAMUP_DESKTOP_RUNTIME = '/home/alice/TeamUp/.venv/bin'
$env:TEAMUP_DESKTOP_CONFIG = '/home/alice/.config/teamup/client.json'
$env:TEAMUP_DESKTOP_STATE_DIR = '/home/alice/.local/state/teamup/launcher'
```

The launcher uses the default WSL distribution when none is selected. WSL localhost forwarding must be enabled. An existing Client started outside this launcher must be stopped or moved to the same launcher-managed startup before opening the app; the app preserves the profile and refuses to take over a live socket. The launch credential travels over a private pipe and stays in Electron's main process.

`TEAMUP_ELECTRON_ZIP_DIR` can point the Windows packager at a directory containing the pinned `electron-vVERSION-win32-x64.zip` for offline packaging. The normal download uses the Electron package's pinned checksums. Run `uv run pytest -q tests/test_desktop_wsl.py` inside WSL to verify the actual bridge into a selected runtime.

## macOS build and open

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
