# Windows desktop validation

Validated on 2026-11-25. Desktop implementation: `665246d7364a6b3f352e2d8b0cf33492fd00361e`.
This includes upstream `main` at `663928132d586ce0245ab3f5098aa8554bbbd08f`
and its versions, room directory, presence ordering and Room Board changes.
The latest upstream release observed during the upgrade was `v0.2.0`.

Environment: Windows x64, WSL2 Ubuntu, Python 3.12.15, Node.js 24.15.0,
npm 11.12.1, Electron 45.5.0. The Windows app is version `0.2.0`.
The WSL Client was upgraded from `0.1.0` to `0.2.1.dev20`, using the
repository's version calculation for merge commit
`71d732ee1f81d4c7dfe35f44ac0ac4c951b24b47`. Client/MCP sources and Python
launchers are unchanged between that merge and the desktop implementation commit.
The configured Server reported `0.2.1.dev26`.

## Automated checks

- `npm --prefix client/desktop test`: 20 passed, 0 skipped. Includes Windows
  literal argument/path handling, invalid distribution/path rejection, macOS
  defaults, protocol/history, stale version endpoint renewal, board routes,
  presence ordering and three real PowerShell installer filesystem checks.
- `pytest -q tests/test_desktop_wsl.py client/tests/test_desktop_launcher.py
  client/tests/test_console.py`: 40 passed, 0 skipped, in the staged candidate's
  installed WSL runtime. The launcher starts and reuses a real Client.
- `pytest -q tests/test_boundaries.py tests/test_mcp_contract.py
  tests/contract/test_mcp_tools.py client/tests/test_task_ipc.py`: 8 passed.
- Ruff check and format check passed for `launch-wsl.py` and
  `tests/test_desktop_wsl.py`. `git diff --check` passed.

The host's `/tmp` tmpfs was full. Python used `TMPDIR=/var/tmp` and an external
collection hook remapping only the launcher's existing explicit
`TemporaryDirectory(dir="/tmp")` fixture to `/var/tmp`. Assertions and Client
authorization were unchanged. An initial run against the previous environment
failed the launcher case (39 passed, 1 failed); the candidate-runtime run passed
all 40. The installer fixture initially compared an 8.3 path with its long
spelling; its assertion now resolves both filesystem paths.

## Package, installation and runtime

- The Windows packager produced the complete portable directory with
  `Install-TeamUp.ps1`. The cached Electron archive matched its pinned SHA-256:
  `20155c8b8aa5b1f575f3757bdf21490e0bac2f19a65a26ce9ba99bcf9f2c69bd`.
- The real installer upgraded the per-user app, updated its desktop shortcut and
  retained the previous app beside it for rollback. Both EXE ProductVersion and
  FileVersion report `0.2.0`. Its explicit `-Start` option uses normal window
  style for the interactive app.
- The WSL runtime was staged separately. Offline dependency resolution could not
  complete, so the isolated environment retained the existing 38 third party
  dependency distributions. Client and MCP were built from the staged sources;
  `uv pip check` passed for all 40 installed distributions.
- Switching the existing managed user service preserved the private profile
  byte-for-byte at the switch, its approved member identity and four rooms.
  The previous runtime and a private service/state backup were retained.
  Local MCP bootstraps now select the current runtime. No Server upgrade was
  performed.

## Live Windows and console checks

- The real window's accessibility tree showed `teamup://console/`, `Live`,
  the existing PR Review transcript, an acknowledged board, member presence,
  Client `0.2.1.dev20` and Server `0.2.1.dev26`. The renderer URL contained
  no launch credential.
- The installed ASAR's main, protocol and launcher modules and five console
  assets were byte-compared with the tested sources. Manifest version/entry
  fields and the bundled installer were checked too.
- Repeated production launcher invocation reused the private console endpoint.
  The production console handler read workspace, versions, room transcript,
  board, member roster and tasks across Windows-to-WSL localhost forwarding:
  all HTTP 200. It observed four rooms, an available room directory, 30 transcript
  messages, 13 members and the two user-requested PR review tasks.
- Fresh MCP stdio sessions listed and invoked current task tools with the
  existing approved member binding. The two requested review tasks were created
  successfully, separately from the read-only package verification.
- Terminating and relaunching only the installed GUI preserved the running WSL
  Client PID. Fresh EXE launch created one window; repeated launch retained one.
- Room-tab clicking was not completed: the automation helper repeatedly reported
  the window minimized after activation. Board, roster and tasks were verified
  through the named console APIs; broader interactive workflows are not claimed.

Portable ZIP: `TeamUp-Windows-x64-0.2.0-20261125.zip`, 158385062 bytes.
SHA-256: `5cce92958edfd5203199a9d37c41a92cf452505a8d51abe9d881aab634bc94cc`.
It contains the full app directory without a private profile or tokens.

## Scope

This preview is unsigned, Windows x64 only, and requires a configured WSL2 Client
and localhost forwarding. The installer updates the Windows app; the WSL runtime
is a separate upgrade. macOS packaging and the full repository suite were not run
on this host. Hosted CI results are reported on the PR.
