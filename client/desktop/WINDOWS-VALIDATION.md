# Windows desktop validation

Validated on 2026-11-25 against base `165bcdd99797dc229714cec012ab2eded8b8656e`.

Environment: Windows x64, WSL2 with Python 3.12.15, Node.js 24.15.0,
npm 11.12.1, Electron 45.5.0. The installed Client supports `serve --console`.

## Automated checks

- `npm --prefix client/desktop test`: 13 passed, 0 skipped. Includes Windows
  argument construction, literal paths containing spaces and shell characters,
  invalid distribution/path rejection, native launcher compatibility, and the
  existing desktop protocol/history checks.
- `pytest -q tests/test_desktop_wsl.py client/tests/test_desktop_launcher.py
  client/tests/test_console.py`: 36 passed, 0 skipped, under WSL2. The bridge
  tests execute a real subprocess into the selected Python runtime.
- `pytest -q tests/test_boundaries.py`: 1 passed.
- Ruff check and format check passed for the two new Python files.
- `git diff --check` passed.

The host's `/tmp` tmpfs was full. The Python run used `TMPDIR=/var/tmp` and an
external pytest collection hook that remapped only the existing desktop
launcher's explicit `TemporaryDirectory(dir="/tmp")` fixture to `/var/tmp`.
The committed test, its assertions, launcher, HTTP authentication and process
reuse checks were unchanged; no tests were skipped.

## Windows package and live connection

- `npm --prefix client/desktop run package:win` produced `TeamUp.exe` and the
  accompanying portable app directory on Windows.
- The Electron runtime archive was checked against the pinned Electron package's
  published SHA-256: `20155c8b8aa5b1f575f3757bdf21490e0bac2f19a65a26ce9ba99bcf9f2c69bd`.
- Installed the full directory and created a desktop shortcut. The real window's
  accessibility tree showed `teamup://console/`, `Live`, the existing review room,
  and its actual transcript. No launch credential appeared in the renderer URL.
- A separate integration check invoked the production Windows launcher twice
  against the installed Python resources and confirmed the same console endpoint
  was reused. It exercised the production console protocol handler from Windows
  to the WSL listener: workspace, acknowledged boards, two authorized room reads,
  30 existing transcript messages and an empty open-task list returned HTTP 200.
  The newly joined room appeared in the workspace. No messages were posted by
  this verification.
- Terminated and relaunched the Windows GUI process; the managed WSL Client
  remained active with the same PID. Opening the EXE again exposed one TeamUp
  window. This lifecycle check used process relaunch, not an automated close-button
  click.

The portable ZIP SHA-256 is
`35747572b538a91919759eca07a126cbe77bf9156da7e014f582d3de7916b175`.
It contains the full app directory, without a private Client profile or tokens.

## Scope

This preview is unsigned, Windows x64 only, and requires an already configured
WSL2 Client runtime and localhost forwarding. It is a Windows desktop shell for
the existing Client, whose Python process remains in WSL2. The macOS package was
not built on this Windows host; native launch defaults are covered by Node tests.
The full repository suite and hosted CI have not been run locally.
