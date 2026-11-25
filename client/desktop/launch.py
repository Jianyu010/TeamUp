"""Hand console addresses to the desktop main process through a private pipe."""

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


def read_profile(config):
    try:
        raw = json.loads(read_private(config, 128 * 1024))
        if not isinstance(raw, dict):
            raise TypeError("Expected an object")
        return raw
    except (FileNotFoundError, ValueError, TypeError) as error:
        raise LaunchError(
            "The selected TeamUp Client configuration is unavailable or invalid."
        ) from error


def configured_socket(config):
    try:
        path = Path(read_profile(config)["socket_path"])
    except (KeyError, TypeError) as error:
        raise LaunchError(
            "The selected TeamUp Client configuration is unavailable or invalid."
        ) from error
    if not path.is_absolute():
        raise LaunchError("The Client configuration must name an absolute socket path.")
    return path.parent.resolve() / path.name


def configured_server_origin(config):
    origin = read_profile(config).get("server_url")
    if not isinstance(origin, str) or not origin:
        raise LaunchError("The selected TeamUp profile must specify a Server origin.")
    return origin


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


def server_url(path):
    with suppress(FileNotFoundError, UnicodeError):
        value = read_private(path, 256).decode().strip()
        match = LOCAL_URL.fullmatch(value)
        if match and 0 < int(match[1]) <= 65535:
            return value
    return None


def accepts_http(value):
    match = LOCAL_URL.fullmatch(value)
    connection = http.client.HTTPConnection("127.0.0.1", int(match[1]), timeout=1)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status == 200 and response.getheader(
            "Content-Type", ""
        ).startswith("text/html")
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


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
        running = live_socket(socket_path)
        if running:
            record = saved_launch(metadata)
            value = server_url(url_file)
            if (
                record.get("config") == str(config)
                and record.get("socket") == list(running)
                and running_pid(record.get("pid"))
                and value
                and accepts_http(value)
            ):
                return value
            raise LaunchError(
                "This TeamUp Client is already running outside the launcher or its Console is unavailable."
            )

        if not client.is_file() or not os.access(client, os.X_OK):
            raise LaunchError(
                "The TeamUp Client runtime is missing from clients/current."
            )
        write_private(metadata, b"")
        with (
            private_file(url_file, writable=True) as output,
            private_file(log_file, writable=True) as log,
        ):
            output.truncate(0)
            log.truncate(0)
            process = subprocess.Popen(
                [str(client), "serve", "--config", str(config), "--console"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=log,
                start_new_session=True,
                cwd=config.parent,
            )
        try:
            write_private(pidfile, str(process.pid).encode())
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and process.poll() is None:
                running = live_socket(socket_path)
                value = server_url(url_file)
                if running and value and accepts_http(value):
                    write_private(
                        metadata,
                        json.dumps(
                            {
                                "config": str(config),
                                "socket": list(running),
                                "pid": process.pid,
                            }
                        ).encode(),
                    )
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
        raise LaunchError(f"The TeamUp Client could not start. See {log_file}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--client", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--server-origin", action="store_true")
    args = parser.parse_args()
    if not args.server_origin and (args.client is None or args.state_dir is None):
        parser.error(
            "--client and --state-dir are required to launch the Client Console"
        )
    os.umask(0o077)
    try:
        mode = os.fstat(sys.stdout.fileno()).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise LaunchError(
                "The Console URL must be handed to the launcher over a private pipe."
            )
        path = args.config.expanduser().absolute()
        if args.server_origin:
            value = json.dumps(configured_server_origin(path))
        else:
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
