import ipaddress
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ._local import LocalError, private_file
from ._protocol import decode_json, is_secret, is_string


class ConfigError(Exception):
    """Invalid TeamUp Client configuration."""


@dataclass(frozen=True)
class Binding:
    binding_key: str = field(repr=False)
    member_token: str = field(repr=False)
    harness_instance_id: str
    session_key: str


@dataclass(frozen=True)
class ClientConfig:
    server_url: str
    socket_path: Path
    enrollment_key: str = field(repr=False)
    bindings: tuple[Binding, ...]
    tls: ssl.SSLContext


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


def load(path):
    try:
        with os.fdopen(private_file(path), "rb") as source:
            data = decode_json(source.read())
    except (OSError, LocalError, ValueError, RecursionError):
        raise ConfigError("Could not read a private JSON configuration file") from None

    required = {"server_url", "socket_path", "enrollment_key", "bindings"}
    if not isinstance(data, dict) or not required <= data.keys() <= required | {
        "ca_file"
    }:
        raise ConfigError("Invalid Client configuration fields")
    server_url = _server_origin(data["server_url"])
    if not is_string(data["socket_path"]) or "\0" in data["socket_path"]:
        raise ConfigError("socket_path must be an absolute path")
    socket_path = Path(data["socket_path"])
    if not socket_path.is_absolute() or not socket_path.name:
        raise ConfigError("socket_path must be an absolute path")

    if not is_secret(data["enrollment_key"]):
        raise ConfigError("Invalid enrollment_key")
    if not isinstance(data["bindings"], list):
        raise ConfigError("bindings must be a list")
    bindings = []
    secrets = {data["enrollment_key"]}
    sessions = set()
    for item in data["bindings"]:
        if not isinstance(item, dict) or set(item) != set(Binding.__dataclass_fields__):
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
        secrets.update(credentials)
        sessions.add(identity)
        bindings.append(binding)

    ca_file = None
    if "ca_file" in data:
        if not is_string(data["ca_file"]) or "\0" in data["ca_file"]:
            raise ConfigError("Invalid ca_file")
        ca_file = path.parent / data["ca_file"]
    try:
        tls = ssl.create_default_context(cafile=ca_file)
    except (OSError, ValueError):
        raise ConfigError("Could not load the TLS trust configuration") from None
    return ClientConfig(
        server_url, socket_path, data["enrollment_key"], tuple(bindings), tls
    )
