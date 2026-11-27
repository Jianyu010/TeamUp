# Windows desktop validation

Validated on 2026-11-27. This revision includes upstream `main` at
`9e4531196f3b05766d38fe2e6cc79a07bca4800a` and resolves the five merge conflicts
in PR #53 while preserving upstream sign-in, wake settings, and room archive UI.

Environment: Windows x64, WSL2 Ubuntu, isolated Python 3.12.15 virtual environment,
Node.js 24.15.0 on Windows and Linux, Electron 45.5.0, and Windows PowerShell.
Candidate Python packages were installed from this checkout. Cached dependency
wheels were checked against the current `uv.lock` SHA-256 hashes before use.

## Automated checks

- `npm --prefix client/windows-wsl/desktop test`: **40 passed, 0 skipped** on
  Windows. Includes three real PowerShell installer checks, WSL argument handling,
  named console routes, cross-origin rejection, login payload adaptation, and the
  shared renderer suite.
- The macOS desktop and shared renderer Node suite: **40 passed, 0 skipped** in
  WSL with Node 24.15.0. Keychain checks use the suite's fake helper; this is not a
  macOS packaging or native Keychain check.
- The following Python selection: **81 passed, 0 skipped** in the isolated WSL
  environment:

  ```sh
  python -m pytest -q tests/client/windows-wsl/desktop \
    tests/client/shared/src/teamup_client/test_console.py \
    tests/client/shared/src/teamup_client/test_task_ipc.py \
    tests/plugin/mcp/src/teamup_mcp/test_mcp_contract.py \
    tests/test_boundaries.py tests/contract tests/system
  ```

  This includes real Client subprocesses for legacy launch, pending sign-in reuse,
  failed credentials, successful sign-in and reuse of the same Client PID. The
  launcher also refuses to start a second Client when its pending console cannot
  be verified.
- Full-tree Ruff check, formatting for Windows Python sources and tests, mypy
  (**33 production source files**), JavaScript syntax, and `git diff --check` pass.

This host's `/tmp` tmpfs is full. The Python selection uses `TMPDIR=/var/tmp` and
an external test bootstrap relocating explicit test `tempfile` requests for
`/tmp` to `/var/tmp`. Production sources and test assertions are unchanged by
that bootstrap. Shared renderer tests run once from their shared test directory;
Windows no longer carries copies of the upstream renderer tests.

## Package and real desktop smoke check

`npm --prefix client/windows-wsl/desktop ci --offline --ignore-scripts` and
`npm --prefix client/windows-wsl/desktop run package:win` pass. The pinned Electron
ZIP was fetched through the verified `hk-leihui` SOCKS tunnel and checked against
`electron/checksums.json`:

`20155c8b8aa5b1f575f3757bdf21490e0bac2f19a65a26ce9ba99bcf9f2c69bd`

The packaged Windows EXE reports ProductVersion and FileVersion `0.2.1`.
Runtime JavaScript, all ten shared console assets, both Python launchers and the
installer match the tested source byte-for-byte. Packaged runtime manifest fields
match the source manifest; Electron Packager removes development-only fields.

The actual packaged EXE was opened with a separate Electron user-data directory,
a private synthetic WSL profile, the candidate Client runtime and a local test
login server. Checks passed for:

- Opening the sign-in form before the Client IPC socket exists.
- A second launch focusing the same single console window.
- Submitting the shared desktop form through the Windows-to-WSL login bridge.
- Opening the authenticated workspace and its registered-session empty state.
- Saving the wake preference through the published console API.
- Keeping the macOS-only Remember me option hidden and renderer Node access disabled.

The smoke fixture was stopped after the check. Package hashes:

| File | SHA-256 |
| --- | --- |
| `resources/app.asar` | `0beed77a6affb80ae1fffe16e6593bca91a68cc6ad823d3b890e59e8d3e9168f` |
| `TeamUp.exe` | `3c60b71ccedc971545ea3efb8e9d96f8548f1ffcd10d02bcc44b5d74517d9500` |

## Scope

The portable Windows build is unsigned and requires WSL2 with localhost
forwarding. The final smoke check uses a synthetic Server and profile. The full
repository pytest suite and macOS packaging were not run locally; repository CI
and the maintainer merge gate remain required.
