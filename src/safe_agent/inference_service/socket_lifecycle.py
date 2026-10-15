"""Linux owner-only UDS ownership journal and exclusive lifetime lock.

A restart may remove ONLY its recorded, refused-connection socket after taking
its durable lock. Unknown paths, changed inodes, live peers and symlinks deny.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
from pathlib import Path
from types import TracebackType


class OwnedUnixListener:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.listener: socket.socket | None = None
        self._fd: int | None = None
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> socket.socket:
        try:
            directory = self.path.parent
            info = directory.lstat()
            if (not self.path.is_absolute() or directory.resolve(strict=True) != directory
                    or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError("socket directory denied")
            lock_path = directory / (self.path.name + ".owner")
            self._fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(self._fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("socket owner journal denied")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prior = os.read(self._fd, 1025)
            if self.path.exists() or self.path.is_symlink():
                existing = self.path.lstat()
                record = json.loads(prior)
                if (not isinstance(record, dict) or set(record) != {"dev", "ino", "uid"}
                        or record != {"dev": existing.st_dev, "ino": existing.st_ino,
                                      "uid": os.getuid()}
                        or not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid()
                        or existing.st_mode & 0o077 or len(prior) > 1024):
                    raise ValueError("unknown socket ownership")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(str(self.path)) != errno.ECONNREFUSED:
                        raise ValueError("socket still active or state unknown")
                final = self.path.lstat()
                if (final.st_dev, final.st_ino) != (existing.st_dev, existing.st_ino):
                    raise ValueError("socket ownership changed")
                self.path.unlink()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            bound = self.path.lstat()
            self._identity = (bound.st_dev, bound.st_ino)
            record_bytes = json.dumps({"dev": bound.st_dev, "ino": bound.st_ino,
                                       "uid": os.getuid()}, sort_keys=True).encode("ascii")
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            if os.write(self._fd, record_bytes) != len(record_bytes):
                raise ValueError("socket ownership write failed")
            os.fsync(self._fd)
            self.listener.listen(32)
            return self.listener
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        try:
            if self.listener is not None:
                self.listener.close()
            if self._identity is not None:
                try:
                    current = self.path.lstat()
                    if (current.st_dev, current.st_ino) == self._identity and stat.S_ISSOCK(current.st_mode):
                        self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
