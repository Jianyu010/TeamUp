# Optional direct HTTPS access for TeamUp

[简体中文](team-https.zh-CN.md)

This is an optional deployment for the [trusted team pilot](team-pilot.md). Caddy terminates TLS on the server, and members on a private network or VPN connect to TeamUp using a stable DNS name. Members can connect without an SSH tunnel. The [Client–Server v1](client-server-v1.md) and [Account login v1](account-login-v1.md) contracts still apply, and TeamUp Server still listens only on loopback. The live pilot continues to use SSH tunnels. Automated acceptance tests (`tests/integration/test_https_proxy.py`) cover this setup on loopback. A live deployment and access from a second device are still unverified.

## Prerequisites and scope

- This deployment is for trusted members on a private network or VPN. Bind to the server's private or VPN interface and keep the service off the public internet and production networks.
- The private network or VPN must resolve a stable DNS name to a fixed server address. Temporary hostnames or rotating addresses invalidate the certificate and Client configuration. Treat a DNS name change as a redeployment.
- TeamUp Server still listens only on `127.0.0.1` and receives requests from the reverse proxy on the same host. The Server retains responsibility for login, member tokens, authorization, and suspension.

## Server configuration

### DNS and certificates

The administrator supplies a matching certificate and private key.

- Everyone uses the same DNS name. It must match `TEAMUP_HTTPS_HOST` (the site's Host matcher) and appear in full in the certificate's subject alternative name (SAN), for example `teamup.corp.example`. A request to a bare IP address will fail the Host match. This setup supports DNS names only and requires no IP entry in the SAN.
- When changing the DNS name or certificate, replace the certificate and matching private key together, then restart Caddy. Replacing either one on its own can cause startup or TLS handshake failures.
- Set the private key's file permissions to `0600` so only the user running Caddy can read it. Keep the key on the server.

### Caddy

`deploy/Caddyfile` reads deployment settings from environment variables.

| Variable | Meaning |
| --- | --- |
| `TEAMUP_HTTPS_HOST` | DNS name members connect to. It must match the certificate and is used for Host validation. |
| `TEAMUP_HTTPS_PORT` | HTTPS listening port, usually 443 in a deployed setup. |
| `TEAMUP_HTTPS_BIND` | Explicit bind address on the server, such as the IP of a private or VPN interface. If unset, Caddy binds to loopback at `127.0.0.1`. If set to an empty string, Caddy rejects the configuration. Neither case binds to all interfaces. Caddy uses an explicit address as supplied. |
| `TEAMUP_TLS_CERT`, `TEAMUP_TLS_KEY` | Absolute paths to the certificate and private key files. |
| `TEAMUP_UPSTREAM_PORT` | TeamUp Server's loopback port (8765 in the default deployment). |

- `admin off` disables the Caddy admin endpoint. `auto_https off` disables automatic certificate management and HTTP redirects. TLS uses the certificate and key you supply.
- The required variables fail closed. Site-level `vars` directives mirror `TEAMUP_HTTPS_HOST`, `TEAMUP_HTTPS_PORT`, `TEAMUP_TLS_CERT`, `TEAMUP_TLS_KEY`, and `TEAMUP_UPSTREAM_PORT`. If any is unset or empty, the corresponding directive has too few arguments and `caddy adapt` exits with an error, rejecting the entire configuration. See the verification details below for the tests on Caddy v2.11.9.
- `TEAMUP_HTTPS_BIND` also fails closed. The Caddyfile expression `{$TEAMUP_HTTPS_BIND:127.0.0.1}` uses the default only when the variable is unset. An explicit empty string expands to an empty argument, leaving the site-level `vars teamup_https_bind` directive short of arguments. Caddy rejects the configuration instead of listening on all interfaces.
- Caddy proxies only `/v1/*` to the literal loopback upstream `127.0.0.1:$TEAMUP_UPSTREAM_PORT` and returns 404 on all other paths.
- Caddy forwards the `Authorization` header unchanged. The backend handles authentication and authorization according to the contract.
- Caddy streams SSE (`/v1/events`) immediately by default. No buffering or redirects are configured. SSE provides hints, and the durable inbox recovers events after a dropped frame. Reconnect behavior stays the same.

Run Caddy under a process manager such as systemd and supply the environment variables through your deployment configuration.

```sh
caddy run --config /opt/teamup-pilot/Caddyfile --adapter caddyfile
```

## Distributing the source

Use the same Client source as the SSH-based deployment. From the repository root, run `git archive` for a pinned revision, record the SHA-256 digest, and distribute the archive through a separate private channel approved by the team. The HTTPS site only proxies `/v1` and cannot serve as a source distribution channel. Members verify the digest before extracting and installing the archive and accept archives only from a known source.

```sh
revision='<pinned-revision>'
stage=$(mktemp -d)
python3 -m scripts.ci.version --local --sha "$revision" > "$stage/TEAMUP-VERSION" &&
  git archive --format=tar.gz --prefix=teamup-account-pilot/ \
    --add-file="$stage/TEAMUP-VERSION" -o teamup-client-src.tar.gz "$revision" &&
  sha256sum teamup-client-src.tar.gz
```

## Connecting as a member

TeamUp account login is the only entry point for members using direct HTTPS. Members need neither a server SSH account nor a tunnel. Server SSH accounts remain within the server administrators' trust boundary. The Client configuration differs from the [trusted team pilot](team-pilot.md) in two settings.

- Set `server_url` to `https://<DNS>:<port>`.
- Add `ca_file` and point it to the private CA certificate distributed by the administrator. The Client uses it to verify the server certificate.

Follow the same `login`, `serve`, and local MCP setup steps afterward.

## Verification and remaining work

Automated acceptance testing runs Caddy on loopback with a temporary private CA and a `localhost` certificate. Supply the Caddy binary through `TEAMUP_CADDY_BIN` or `PATH`, then run `uv run --no-sync pytest tests/integration/test_https_proxy.py`. The suite covers the following behavior.

- Account login and Agent registration through the proxy.
- Real-time SSE delivery in a separate check. An independently authenticated SSE stream through the proxy waits for a real `: ready` frame. Another handle sends an @mention, and the same stream receives the complete `inbox_changed` frame within the time limit.
- Desktop notifications for an online member and inbox catch-up when an offline member reconnects. These assertions are separate from the SSE check.
- TLS verification with the wrong CA, rejection of an incorrect password, and 404 responses on paths outside `/v1`.

The same suite runs `caddy adapt` to check all three bind settings. An unset `TEAMUP_HTTPS_BIND` binds only to loopback, an explicit empty string causes the configuration to be rejected, and an explicit address is preserved as supplied. The suite also verifies that an unset or empty value for any required variable causes the entire configuration to be rejected. These checks ran against Caddy v2.11.9.

A live private-network or VPN deployment, a connection across network segments, access from a second device, and production networks are still unverified. The live pilot keeps SSH tunnels as its default until those checks are complete and the owners approve the change.
