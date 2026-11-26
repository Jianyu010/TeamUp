import asyncio
import fcntl
import os
import socket
import stat
import struct
import sys
from contextlib import asynccontextmanager, contextmanager

from ._protocol import REQUEST_LIMIT


class LocalError(Exception):
    """An unusable or untrusted local endpoint."""


def private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    entry = path.lstat()
    if (
        not stat.S_ISDIR(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or entry.st_mode & 0o077
    ):
        raise LocalError(
            "Socket directory must be owned by the current user and private"
        )


def private_file(path, *, create=False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_RDWR
    fd = os.open(path, flags, 0o600)
    try:
        entry = os.fstat(fd)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or entry.st_mode & 0o077
        ):
            raise LocalError(
                "File must be regular, owned by the current user, and private"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def same_user(peer):
    try:
        if sys.platform.startswith("linux"):
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("=iII", credentials)
        elif sys.platform == "darwin":
            credentials = peer.getsockopt(0, socket.LOCAL_PEERCRED, 256)
            _, uid = struct.unpack_from("=II", credentials)
        else:
            return False
    except (AttributeError, OSError, struct.error):
        return False
    return uid == os.geteuid()


def private_socket(path):
    entry = path.lstat()
    if (
        not stat.S_ISSOCK(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or entry.st_mode & 0o077
    ):
        raise LocalError("Socket must be owned by the current user and private")
    return entry


def private_forward_socket(path):
    """Resolve a socket through directories that other users cannot replace."""
    resolved = path.resolve(strict=True)
    for directory in reversed(resolved.parents):
        entry = directory.lstat()
        if (
            not stat.S_ISDIR(entry.st_mode)
            or entry.st_uid not in {0, os.geteuid()}
            or (entry.st_mode & 0o022 and not entry.st_mode & stat.S_ISVTX)
        ):
            raise LocalError("Server socket path includes an untrusted directory")
    private_directory(resolved.parent)
    private_socket(resolved)
    return resolved


def _remove_stale_socket(path):
    try:
        private_socket(path)
    except FileNotFoundError:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
            return
    raise LocalError("Client socket is already in use")


@contextmanager
def lock_endpoint(path, *, remove_stale_socket):
    private_directory(path.parent, create=True)
    fd = private_file(path.with_name(f"{path.name}.lock"), create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LocalError("Client socket is already in use") from None
        if remove_stale_socket:
            _remove_stale_socket(path)
        yield
    finally:
        os.close(fd)


@asynccontextmanager
async def listen(path, handle):
    clients = set()

    def connected(reader, writer):
        task = asyncio.create_task(handle(reader, writer))
        clients.add(task)
        task.add_done_callback(clients.discard)

    entry = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(str(path))
            entry = path.lstat()
            path.chmod(0o600)
            listener.listen(socket.SOMAXCONN)
            server = await asyncio.start_unix_server(
                connected, sock=listener, limit=REQUEST_LIMIT
            )
            try:
                yield
            finally:
                server.close()
                for task in clients:
                    task.cancel()
                await asyncio.gather(*clients, return_exceptions=True)
                await server.wait_closed()
        finally:
            if entry is not None:
                try:
                    current = path.lstat()
                    if (entry.st_dev, entry.st_ino) == (current.st_dev, current.st_ino):
                        path.unlink()
                except FileNotFoundError:
                    pass
