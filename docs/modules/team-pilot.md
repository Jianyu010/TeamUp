# TeamUp pilot for trusted teams

[简体中文](team-pilot.zh-CN.md)

The pilot host provides access to TeamUp through an SSH tunnel. Each developer signs in with an individual TeamUp account. The host's SSH account is part of the trust boundary. Server administrators using a shared account can access TeamUp runtime data.

For access without a tunnel, see [Direct HTTPS access](team-https.md). The live pilot still uses SSH tunneling. Direct HTTPS has yet to be verified in a live deployment.

## Provision accounts

Run the following commands on the server and assign each person a TeamUp username. When creating an account or resetting a password, enter the temporary password at the terminal and deliver it to the account holder through the team's agreed private channel.

```sh
sudo -u teamup-pilot /opt/teamup-pilot/bin/teamup-server accounts --db /var/lib/teamup-pilot/teamup.db create alice
sudo -u teamup-pilot /opt/teamup-pilot/bin/teamup-server accounts --db /var/lib/teamup-pilot/teamup.db list
```

The same command supports `reset-password alice` and `disable alice`. Disabling an account revokes both sign-in access and that account's agent credentials.

## Connect and install the Client

On the Mac or Linux machine running the Client, start the SSH tunnel in a terminal and leave it running. On Windows, start the tunnel in the same WSL2 distribution as the Client.

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18765:127.0.0.1:8765 ubuntu@SSH_HOST
```

In a second local terminal, retrieve and install the matching source distribution. Then generate a registration entry for your local Claude Code installation.

```sh
scp ubuntu@SSH_HOST:/opt/teamup-pilot/teamup-client-src.tar.gz .
tar -xzf teamup-client-src.tar.gz
cd teamup-account-pilot
uv sync --locked --all-packages --python 3.12
uv run --no-sync teamup-client init-install claude-code-local
```

Follow the [Client configuration example](quickstart.md#start-the-client) to add the generated entry to `installs` in your private JSON file with mode `0600`. Set `server_url` to `http://127.0.0.1:18765` and omit `enrollment_key`. Open TeamUp.app and enter your TeamUp username and password in the console. To sign in with a browser, run `uv run --no-sync teamup-client serve --config /private/path/client.json --console` and open the loopback URL it prints.

The macOS console's **Remember me on this Mac** option requires a verified HTTPS server or a private Unix socket forward. Keep your existing `server_url`. If you use an SSH tunnel, add a local socket forward in a directory accessible only to your user. The command below shows how on macOS. If other tools still need TCP forwarding, keep the original `-L 127.0.0.1:18765:127.0.0.1:8765` option on the same SSH command.

```sh
ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes \
  -L "$HOME/.config/teamup/server.sock:127.0.0.1:8765" ubuntu@SSH_HOST
```

Add `"server_socket": "/absolute/path/to/server.sock"` to your private Client JSON, restart the Client, and sign in. Your user must own both the socket and its immediate parent directory, and neither can be accessible to other users.

For a headless terminal or background deployment, use the following commands.

```sh
uv run --no-sync teamup-client login --config /private/path/client.json --username alice
uv run --no-sync teamup-client serve --config /private/path/client.json
```

Follow the [local MCP setup instructions](quickstart.md#set-up-local-mcp) to configure stdio MCP once for your Claude Code installation. Each session registers and is approved separately. Resuming a session reuses its member identity. If the human sign-in token expires, sign in again when you need to register a new session.
