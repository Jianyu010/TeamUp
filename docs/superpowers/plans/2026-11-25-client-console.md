# TeamUp Client Console Implementation Plan

**Goal** Bring the approved workspace and room navigation into the running Client using existing authorized room data.

**Architecture** Add one optional loopback browser listener at the Client composition root. A Client-owned read projection supplies the workspace from live, stable local sessions. The Server continues to authorize each member read through its published Gateway.

**Tech stack** Python stdlib HTTP listener, existing async Client upstream, packaged HTML, CSS and JavaScript.

- [x] Add focused tests in `client/tests/test_console.py` for participant-scoped room history, removal during a read, application credentials, and packaging-visible assets. Run `uv run --all-packages --no-sync pytest client/tests/test_console.py -q`.
- [x] Add `_console_data.py` and an approved-session enumeration method in `_coordinator.py`. Fetch joined-room summaries per session and fold by Server room ID. Use the caller's chosen session for recent messages. Keep any Board read or write outside this projection.
- [x] Add `_console.py` and packaged files under `client/src/teamup_client/console/`. Serve only the expected loopback hostname and require the launch credential before serving data. Wire the optional `serve --console` flag through `main.py`.
- [x] Launch a local Server and Client against registered fixtures. Verify two memberships in one room, transcript scope and the browser room slide. Run `uv run --all-packages --no-sync ruff check .` and the Client, architecture and install tests. Build the Client wheel and inspect its console assets.
