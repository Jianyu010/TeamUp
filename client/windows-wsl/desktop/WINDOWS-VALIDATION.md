# Windows desktop validation

Validated on 2026-11-26 against implementation `d58c804ddd85451da9c824d4489711af95e57155`.
The branch includes upstream `main` at `b9320e4350394c68ddc0a8864ba23b8a008267c4`
and release `v0.2.1`, including member identities, membership controls, retirement,
and the new room-message layout.

The Windows desktop is version `0.2.1`. Its Client reports `0.2.1.dev24`, calculated
by the repository's first-parent version function for this feature branch. The
configured Server reported `0.2.1.dev38` and was not upgraded by this work.
Environment: Windows x64, WSL2 Ubuntu, Python 3.12.15, Node.js 24.15.0,
Electron 45.5.0. Installer filesystem tests use Windows PowerShell.

## Layout and scope

Windows desktop code, its WSL launcher, installer and packager are under
`client/windows-wsl/desktop/`. Platform tests mirror that directory under
`tests/client/windows-wsl/desktop/`, following the desktop owner's request on
[PR #53](https://github.com/Jianyu010/TeamUp/pull/53#issuecomment-7180173760).
`client/windows/` is reserved for a native Windows Client.

At the tested upstream revision, the shared Python Client and console renderer
still live in `client/src/`; the packager reads those shared assets. The planned
upstream move to `client/shared/` and `client/macos/` had not landed on main.
The Windows adapter uses the existing Client console API. The local compact
session-list changes were carried forward with upstream's member identities,
retirement selection and archive controls retained.

## Automated checks

- `npm --prefix client/windows-wsl/desktop test`: **38 passed, 0 skipped**.
  Covers literal WSL arguments, invalid path/distribution rejection, three real
  PowerShell installer checks, console authorization and forwarding, history,
  presence, messages, task views, and session filtering/search/pagination.
- `npm --prefix client/desktop test`: **24 passed, 0 skipped** for the shared
  renderer and existing desktop protocol.
- In the isolated candidate WSL environment, the following pytest selection:
  **72 passed, 0 skipped**:

  ```sh
  python -m pytest -q tests/client/windows-wsl/desktop \
    client/tests/test_console.py client/tests/test_task_ipc.py \
    tests/test_boundaries.py tests/test_mcp_contract.py tests/contract tests/system
  ```

- Full-tree Ruff check, Ruff formatting for the Windows Python launcher and tests,
  mypy (28 production source files), JavaScript syntax and `git diff --check`: pass.

This host's `/tmp` tmpfs was full. The initial cross-process run failed with
`database or disk is full`. The final run used `TMPDIR=/var/tmp` and an external
test bootstrap redirecting explicit `tempfile.mkdtemp(dir="/tmp")` and
`TemporaryDirectory(dir="/tmp")` calls to `/var/tmp`. Test assertions and
production authorization were unchanged. The final 72 checks all passed.

## Package and installed runtime

- `npm --prefix client/windows-wsl/desktop ci --offline --ignore-scripts` and
  `npm --prefix client/windows-wsl/desktop run package:win` completed. Packaging
  used the cached Electron ZIP with pinned SHA-256
  `20155c8b8aa5b1f575f3757bdf21490e0bac2f19a65a26ce9ba99bcf9f2c69bd`.
- Missing Python dependencies were fetched through the verified `hk-leihui`
  SOCKS tunnel and matched against `uv.lock` hashes. A fresh candidate venv
  installed the locked requirements and source-built Client, MCP and Server
  distributions; `uv pip check` passed for all 49 installed distributions.
- The per-user installer upgraded the app and shortcut, retained the previous
  app directory, and launched TeamUp.exe. ProductVersion and FileVersion are
  `0.2.1`. The installed main/protocol/command modules, all ten console assets,
  launchers and installer match the tested sources. Packaged manifest identity,
  version, entry point and engine fields match too.
- The existing managed WSL service was switched with a private state backup and
  retained the previous runtime for rollback. The profile was byte-identical at
  the switch; existing session identities, credentials and rooms were preserved.
  A fresh MCP stdio session listed 25 tools, including room departure and identity
  retirement. Tool discovery sent zero member operations.
- Repeated production launcher calls reused the same private console endpoint.
  Real Windows-to-WSL protocol reads of versions, workspace, transcript, board,
  member roster, tasks and team directory returned HTTP 200 with ready state.

## Desktop interaction

The installed window's accessibility state showed Live, **My sessions (3)**,
**All sessions (277)** and Client `0.2.1.dev24`. The Windows input helper reported
`coordinate input geometry is unavailable` and a minimized window during click
attempts, so interaction checks used an isolated installed Electron process and
its temporary loopback DevTools endpoint.

The final desktop smoke test passed: eight rows per page, distinct second-page
rows, search and empty results, retirement selection cleared by search, PR Review
transcript and member roster loading, authorized Room Board expansion, task lanes,
Team directory, and return to the default compact workspace. The final inventory
contained 280 approved sessions and three registered sessions. The renderer stayed
at `teamup://console/` with Node integration disabled. The isolated process was
closed after testing; the ordinary app remained available.

Portable ZIP: `TeamUp-Windows-x64-0.2.1-20261126.zip`, **162696984 bytes**.
SHA-256: `eb66ca0d83d4249539bfb30983baabbae19d4e170392ec7e96ba26c585da21d5`.
It contains the complete app directory. This unsigned x64 build requires a
configured WSL2 Client and localhost forwarding. macOS packaging and the full
repository pytest suite were not run locally; hosted checks are reported on the PR.
