import base64
import hashlib
import hmac
import ipaddress
import json
import os
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from ._local import LocalError, lock_endpoint, private_file
from ._protocol import decode_json, is_secret, is_string, is_uuid


class ConfigError(Exception):
    """Invalid TeamUp Client configuration."""


@dataclass(frozen=True)
class Binding:
    binding_key: str = field(repr=False)
    member_token: str = field(repr=False)
    harness_instance_id: str
    session_key: str
    codex_install: str | None = None


@dataclass(frozen=True)
class Install:
    """A harness install whose sessions each receive their own member."""

    binding_key: str = field(repr=False)
    harness_instance_id: str
    auto_approve: bool = False


@dataclass(frozen=True)
class ClientConfig:
    server_url: str
    socket_path: Path
    enrollment_key: str | None = field(repr=False)
    account_token: str | None = field(repr=False)
    bindings: tuple[Binding, ...]
    tls: ssl.SSLContext
    notify_command: tuple[str, ...] | None = None
    installs: tuple[Install, ...] = ()
    codex_command: tuple[str, ...] | None = None


def _server_origin(value):
    if not is_string(value) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ConfigError("server_url must be a loopback HTTP or HTTPS origin")
    try:
        origin = urlsplit(value)
        host, port = origin.hostname, origin.port
    except ValueError:
        raise ConfigError("Invalid server_url") from None
    if (
        origin.scheme not in {"http", "https"}
        or not host
        or origin.username is not None
        or port == 0
        or origin.path not in {"", "/"}
        or any(char in value for char in "?#\\")
    ):
        raise ConfigError("server_url must be a loopback HTTP or HTTPS origin")
    if origin.scheme == "http" and host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ConfigError("HTTP server_url must use a loopback address")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[origin.scheme]:
        authority += f":{port}"
    return f"{origin.scheme}://{authority}"


def _read_contents(path):
    try:
        with os.fdopen(private_file(path), "rb") as source:
            return source.read()
    except (OSError, LocalError):
        raise ConfigError("Could not read a private JSON configuration file") from None


def _read(path):
    contents = _read_contents(path)
    try:
        data = decode_json(contents)
    except (ValueError, RecursionError):
        raise ConfigError("Could not read a private JSON configuration file") from None

    required = {"server_url", "socket_path", "bindings"}
    optional = {
        "ca_file",
        "account_token",
        "enrollment_key",
        "notify_command",
        "codex_command",
        "installs",
    }
    if not isinstance(data, dict) or not required <= data.keys() <= required | optional:
        raise ConfigError("Invalid Client configuration fields")
    return data, contents


def _socket_path(value):
    if not is_string(value) or "\0" in value:
        raise ConfigError("socket_path must be an absolute path")
    socket_path = Path(value)
    if not socket_path.is_absolute() or not socket_path.name:
        raise ConfigError("socket_path must be an absolute path")
    return socket_path


def _parse(path, data):
    socket_path = _socket_path(data["socket_path"])
    server_url = _server_origin(data["server_url"])
    enrollment_key = data.get("enrollment_key")
    account_token = data.get("account_token")
    if "account_token" in data and "enrollment_key" in data:
        raise ConfigError("Use either account_token or enrollment_key")
    for name in ("account_token", "enrollment_key"):
        if name in data and not is_secret(data[name]):
            raise ConfigError(f"Invalid {name}")
    if not isinstance(data["bindings"], list):
        raise ConfigError("bindings must be a list")
    bindings = []
    secrets = {enrollment_key, account_token} - {None}
    sessions = set()
    codex_threads = set()
    binding_fields = set(Binding.__dataclass_fields__) - {"codex_install"}
    for item in data["bindings"]:
        if not isinstance(item, dict) or not (
            binding_fields <= item.keys() <= binding_fields | {"codex_install"}
        ):
            raise ConfigError("Invalid binding fields")
        binding = Binding(**item)
        identity = (binding.harness_instance_id, binding.session_key)
        if not (
            is_secret(binding.binding_key)
            and is_secret(binding.member_token)
            and is_string(binding.harness_instance_id, 128)
            and is_string(binding.session_key, 128)
        ):
            raise ConfigError("Invalid binding")
        credentials = {binding.binding_key, binding.member_token}
        if len(credentials) != 2 or secrets & credentials or identity in sessions:
            raise ConfigError("Binding secrets and session identities must be unique")
        if "codex_install" in item:
            if not is_string(binding.codex_install, 128) or not is_uuid(binding.session_key):
                raise ConfigError("A Codex mapping needs an installation ID and a thread UUID")
            thread = (binding.codex_install, binding.session_key.lower())
            if thread in codex_threads:
                raise ConfigError("A Codex installation must map each thread UUID at most once")
            codex_threads.add(thread)
        secrets.update(credentials)
        sessions.add(identity)
        bindings.append(binding)

    installs = []
    if not isinstance(data.get("installs", []), list):
        raise ConfigError("installs must be a list")
    for item in data.get("installs", []):
        if not (
            isinstance(item, dict)
            and {"binding_key", "harness_instance_id"}
            <= item.keys()
            <= {"binding_key", "harness_instance_id", "auto_approve"}
            and is_secret(item["binding_key"])
            and is_string(item["harness_instance_id"], 128)
            and type(item.get("auto_approve", False)) is bool
        ):
            raise ConfigError("Invalid install")
        install = Install(**item)
        if (
            install.binding_key in secrets
            or any(install.harness_instance_id == harness for harness, _ in sessions)
            or any(
                install.harness_instance_id == other.harness_instance_id
                for other in installs
            )
        ):
            raise ConfigError("Install secrets and harness instances must be unique")
        secrets.add(install.binding_key)
        installs.append(install)

    if {install for install, _ in codex_threads} - {
        install.harness_instance_id for install in installs
    }:
        raise ConfigError("codex_install must name a configured installation")

    ca_file = None
    if "ca_file" in data:
        if not is_string(data["ca_file"]) or "\0" in data["ca_file"]:
            raise ConfigError("Invalid ca_file")
        ca_file = path.parent / data["ca_file"]
    try:
        tls = ssl.create_default_context(cafile=ca_file)
    except (OSError, ValueError):
        raise ConfigError("Could not load the TLS trust configuration") from None
    notify_command, codex_command = (
        _argv(data, name) for name in ("notify_command", "codex_command")
    )
    return ClientConfig(
        server_url,
        socket_path,
        enrollment_key,
        account_token,
        tuple(bindings),
        tls,
        notify_command,
        tuple(installs),
        codex_command,
    )


def _argv(data, name):
    if name not in data:
        return None
    command = data[name]
    if not isinstance(command, list) or not all(
        is_string(part, 4096) and "\0" not in part for part in command
    ):
        raise ConfigError(f"{name} must be a list of strings")
    return tuple(command)


def owner_key(config):
    """The owner's IPC credential, derived from the account or enrollment secret.

    Agents hold only binding or install secrets, so they cannot derive it.
    """
    secret = config.account_token or config.enrollment_key
    if secret is None:
        return None
    digest = hmac.new(secret.encode(), b"teamup-owner-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def read_config(path):
    """Parse the configuration without taking the Client lock (read-only use)."""
    data, _ = _read(path)
    return _parse(path, data)


def read_locked_profile(path, socket_path):
    """Read a profile without releasing the lock for its configured socket."""
    data, contents = _read(path)
    config = _parse(path, data)
    if config.socket_path != socket_path:
        raise ConfigError("Client socket path changed while opening the configuration")
    return config, data, contents


@contextmanager
def locked_config(path, *, remove_stale_socket=False):
    original, original_contents = _read(path)
    config = _parse(path, original)
    with lock_endpoint(config.socket_path, remove_stale_socket=remove_stale_socket):
        # Login may have replaced the configuration before we acquired the lock.
        data, contents = _read(path)
        if contents != original_contents:
            updated = _parse(path, data)
            if updated.socket_path != config.socket_path:
                raise ConfigError(
                    "Client socket path changed while opening the configuration"
                )
            config = updated
        yield config, data, contents


def verify_config_unchanged(path, contents):
    if _read_contents(path) != contents:
        raise ConfigError("Client configuration changed during login. Please try again")


def save_account_token(path, config, data, contents, token):
    if any(
        token in (binding.binding_key, binding.member_token)
        for binding in config.bindings
    ) or any(token == install.binding_key for install in config.installs):
        raise ConfigError("Account token conflicts with an existing binding secret")
    updated = {key: value for key, value in data.items() if key != "enrollment_key"}
    updated["account_token"] = token
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".teamup-config-",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            try:
                os.fchmod(target.fileno(), 0o600)
                json.dump(updated, target, indent=2)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
                verify_config_unchanged(path, contents)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    except OSError:
        raise ConfigError("Could not save the private Client configuration") from None
