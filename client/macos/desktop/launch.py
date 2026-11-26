"""Hand a launcher-managed Client console address to the desktop main process."""

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

LOCAL_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/#([A-Za-z0-9_-]{43})")
LOGIN_SOCKET = re.compile(r"/tmp/teamup-console-[a-z0-9_]+/login\.sock")


class LaunchError(Exception):
    pass


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise LaunchError("The TeamUp launcher state folder must be private.")


@contextmanager
def private_file(path, *, writable=False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= os.O_RDWR | os.O_CREAT if writable else os.O_RDONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise LaunchError("A TeamUp launcher file is not private.")
        with os.fdopen(
            descriptor, "r+b" if writable else "rb", closefd=False
        ) as stream:
            yield stream
    finally:
        os.close(descriptor)


def read_private(path, size):
    with private_file(path) as stream:
        return stream.read(size)


def write_private(path, content):
    with private_file(path, writable=True) as stream:
        stream.truncate(0)
        stream.write(content)
        stream.flush()


def configured_socket(config):
    try:
        raw = json.loads(read_private(config, 128 * 1024))
        path = Path(raw["socket_path"])
    except (FileNotFoundError, ValueError, KeyError, TypeError) as error:
        raise LaunchError(
            "The selected TeamUp Client configuration is unavailable or invalid."
        ) from error
    if not path.is_absolute():
        raise LaunchError("The Client configuration must name an absolute socket path.")
    return path.parent.resolve() / path.name


def live_socket(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise LaunchError("The configured Client socket is not a private Unix socket.")
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return None
    return info.st_dev, info.st_ino


def server_endpoint(path):
    with suppress(FileNotFoundError, UnicodeError):
        value = read_private(path, 256).decode().strip()
        lines = value.splitlines()
        if not 1 <= len(lines) <= 2:
            return None
        match = LOCAL_URL.fullmatch(lines[0])
        if not match or not 0 < int(match[1]) <= 65535:
            return None
        if len(lines) == 2:
            if not LOGIN_SOCKET.fullmatch(lines[1]):
                return None
            login = Path(lines[1])
            directory = login.parent.lstat()
            entry = login.lstat()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or not stat.S_ISSOCK(entry.st_mode)
                or any(
                    info.st_uid != os.getuid() or info.st_mode & 0o077
                    for info in (directory, entry)
                )
            ):
                return None
        return value
    return None


def console_state(endpoint):
    value, *private = endpoint.splitlines()
    match = LOCAL_URL.fullmatch(value)
    connection = http.client.HTTPConnection("127.0.0.1", int(match[1]), timeout=3)
    headers = {"Authorization": f"Bearer {match[2]}"}
    try:
        path = "/api/login" if private else "/api/versions"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "")
        if response.status == 200 and content_type.startswith("application/json"):
            data = json.loads(response.read(4096))
            if not isinstance(data, dict):
                return None
            if private and data.get("state") in ("sign_in_required", "ready"):
                return data["state"]
            if not private and set(data) == {"client_version", "server_version", "server_state"}:
                return "legacy"
    except (OSError, http.client.HTTPException, ValueError):
        pass
    finally:
        connection.close()
    return None


def saved_launch(path):
    with suppress(FileNotFoundError, ValueError):
        value = json.loads(read_private(path, 4096))
        if isinstance(value, dict):
            return value
    return {}


def running_pid(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_console(config, client, state_dir):
    socket_path = configured_socket(config)
    private_directory(state_dir)
    key = hashlib.sha256(os.fsencode(socket_path)).hexdigest()[:24]
    profile_dir = state_dir / key
    private_directory(profile_dir)
    metadata = profile_dir / "endpoint.json"
    pidfile = profile_dir / "client.pid"
    url_file = profile_dir / "console.url"
    log_file = profile_dir / "client.log"

    with private_file(profile_dir / "launch.lock", writable=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        def record_endpoint(running, pid):
            write_private(
                metadata,
                json.dumps({
                    "config": str(config),
                    "socket": list(running) if running else None,
                    "pid": pid,
                }).encode(),
            )

        record = saved_launch(metadata)
        value = server_endpoint(url_file)
        running = live_socket(socket_path)
        if (
            value
            and record.get("config") == str(config)
            and running_pid(record.get("pid"))
            and (record.get("socket") is None or record["socket"] == list(running or ()))
        ):
            status = console_state(value)
            running = live_socket(socket_path)
            if record.get("socket") is None and running is None:
                if status == "sign_in_required":
                    return value
                raise LaunchError(
                    "TeamUp Client is still waiting for sign-in but its Console did not respond. "
                    "Try opening TeamUp again."
                )
            if running and status in {"ready", "legacy"}:
                if record.get("socket") == list(running):
                    return value
                if status == "ready" and record.get("socket") is None:
                    record_endpoint(running, record["pid"])
                    return value
        if live_socket(socket_path):
            raise LaunchError(
                "This TeamUp Client is already running outside the launcher or its Console is unavailable."
            )

        if not client.is_file() or not os.access(client, os.X_OK):
            raise LaunchError(
                "The TeamUp Client runtime is missing from clients/current."
            )
        def start_client(flag):
            with (
                private_file(url_file, writable=True) as output,
                private_file(log_file, writable=True) as log,
            ):
                output.truncate(0)
                log.truncate(0)
                return subprocess.Popen(
                    [str(client), "serve", "--config", str(config), flag],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=log,
                    start_new_session=True,
                    cwd=config.parent,
                )

        write_private(metadata, b"")
        for flag in ("--desktop-console", "--console"):
            process = start_client(flag)
            try:
                write_private(pidfile, str(process.pid).encode())
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and process.poll() is None:
                    value = server_endpoint(url_file)
                    if value:
                        status = console_state(value)
                        running = live_socket(socket_path)
                        if status == "sign_in_required":
                            record_endpoint(None, process.pid)
                            return value
                        if running and status in {"ready", "legacy"}:
                            record_endpoint(running, process.pid)
                            return value
                    time.sleep(0.05)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                raise
            if process.poll() is None:
                process.terminate()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            if not (
                flag == "--desktop-console" and process.returncode == 2
                and b"unrecognized arguments: --desktop-console" in read_private(log_file, 4096)
            ):
                break
        raise LaunchError(f"The TeamUp Client could not start. See {log_file}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        mode = os.fstat(sys.stdout.fileno()).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise LaunchError(
                "The Console URL must be handed to the launcher over a private pipe."
            )
        path = args.config.expanduser().absolute()
        value = ensure_console(
            path,
            args.client.expanduser().absolute(),
            args.state_dir.expanduser().absolute(),
        )
        print(value, flush=True)
    except LaunchError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print(
            "TeamUp could not access the selected profile or its local Console.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
