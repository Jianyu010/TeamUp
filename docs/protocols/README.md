# TeamUp development protocol

This protocol applies to all developers and agents. See [AGENTS.md](../../AGENTS.md) for the repository's invariants.

## Changing contracts

1. Read the current feature's specification, architecture, and module contracts to identify what will be affected and who owns it. Use the directory layout to find code and the contracts to identify public entry points.
2. Document changes to public fields, authorization, errors, events, retries, or data ownership in the relevant contract first. Record the use case, provider, affected callers, and compatibility implications.
3. Change the implementation after the provider and affected caller owners have approved the contract. If the required interface does not yet exist, define it in the contract before using it from the caller.

## Client platforms and test layout

- Put the cross-platform Client runtime and static console assets in `client/shared/`. Put platform-specific code in `client/macos/`, `client/windows/`, or `client/windows-wsl/`. The WSL-dependent Windows client belongs in `client/windows-wsl/`, including its Windows launcher and installer.
- Put tests and test-only assets in the repository's top-level `tests/` directory. For tests of a single component, mirror the source tree. For example, `client/macos/desktop/` maps to `tests/client/macos/desktop/`, and `server/src/teamup_server/` maps to `tests/server/src/teamup_server/`.
- Organize cross-component integration, system, contract, installation, and scenario tests by suite under `tests/`.

## Verification and status

- Record proposals, agreed designs, implemented behavior, and observed results. Include the environment and reproducible steps for observed results.
- When dependency boundaries change, check cross-component imports and public entry points. When authorization or state semantics change, verify normal operation, attempts across permission boundaries, retries, and relevant concurrent operations.
- Update the affected specifications, architecture documents, and module documents in the same change. Record accepted rules in the official document that governs them.
