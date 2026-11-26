# TeamUp account sign-in and pilot implementation plan

The [account sign-in contract](../../modules/account-login-v1.md) covers administrative commands, interfaces, permissions, and deployment.

1. Add failing tests under `tests/server/src/teamup_server/` for account provisioning, distinct identities, sign-in and token isolation, rejection of reads and writes after deactivation, and migration from the existing database. Have Collaboration own account state through its public interface in `server/src/teamup_server/`. Add the Gateway API and local administration commands.
2. Add failing tests under `tests/client/shared/src/teamup_client/` for saving sign-in credentials, configuration file permissions, restoring existing identities, and returning errors. Integrate with the existing HTTP client and IPC under `client/shared/src/teamup_client/`. Run `uv run --no-sync pytest -q` to check for regressions in existing flows.
3. Add an end-to-end account acceptance test using the actual `teamup-server`, `teamup-client`, and `teamup-mcp` entry points and two developer bindings. Run it locally, then repeat it through the pilot host's SSH tunnel.
4. Build a Server wheel from the actual commit and install it on the Linux pilot host as a loopback service. Prepare instructions for account administration, SSH tunneling, and Client installation. Keep password rotation and service shutdown behind authorized administrative entry points.
