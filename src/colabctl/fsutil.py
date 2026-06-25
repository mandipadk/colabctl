"""Filesystem primitives shared by the durable stores: atomic writes + advisory locks.

Both the state store (``colabctl.state.store``) and the encrypted secret store
(``colabctl.secrets.encrypted_file``) persist a single document via a read-modify-write
cycle, and both need the same two guarantees:

* **Atomic** — writes go to a temp file in the same directory, are ``fsync``'d, then
  ``os.replace``'d into place, so a reader (or a crash mid-write) never sees a half-written
  file. The destination is left ``0600`` and its parent directory ``0700``.
* **Cross-process safe** — a read-modify-write is wrapped in an exclusive advisory lock
  (the cross-platform ``filelock`` package), released automatically if the holder dies.

Keeping these in one place means the crash-safety and concurrency guarantees are written
and tested once, not re-derived (and subtly broken) per store.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from types import TracebackType

from filelock import FileLock as _PortableLock


class FileLock:
    """A cross-platform exclusive advisory lock scoped to a ``with`` block.

    Backed by the ``filelock`` package, so it works on Windows too — unlike the old POSIX
    ``flock`` version, which silently no-op'd off-POSIX and made cross-process state (the
    state store + encrypted secret store) unsafe there. Released automatically on exit or if
    the holder process dies.

    ``exclusive`` is kept for API compatibility and to signal read-vs-write intent, but
    ``filelock`` always acquires an *exclusive* lock; the POSIX version's shared-read mode is
    dropped (reads are rare and fast for single-user tooling, and serializing a read against a
    concurrent write is the correct behaviour). ``timeout`` (seconds; ``-1`` = block forever)
    bounds the wait — a positive value raises ``filelock.Timeout`` if the lock is still held.
    """

    def __init__(self, path: Path, *, exclusive: bool = True, timeout: float = -1.0) -> None:
        self._path = path
        self._exclusive = exclusive
        self._lock = _PortableLock(str(path), timeout=timeout)

    def __enter__(self) -> FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()


def atomic_write(path: Path, data: str, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``fsync`` + ``os.replace``).

    The parent directory is created ``0700`` and the destination ends up ``mode``
    (default ``0600``), set on the temp file *before* any bytes are written so there is
    no window in which the file is more permissive than intended.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


__all__ = ["FileLock", "atomic_write"]
