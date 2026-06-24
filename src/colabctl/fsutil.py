"""Filesystem primitives shared by the durable stores: atomic writes + advisory locks.

Both the state store (``colabctl.state.store``) and the encrypted secret store
(``colabctl.secrets.encrypted_file``) persist a single document via a read-modify-write
cycle, and both need the same two guarantees:

* **Atomic** — writes go to a temp file in the same directory, are ``fsync``'d, then
  ``os.replace``'d into place, so a reader (or a crash mid-write) never sees a half-written
  file. The destination is left ``0600`` and its parent directory ``0700``.
* **Cross-process safe** — a read-modify-write is wrapped in an exclusive advisory lock
  (POSIX ``flock``), released automatically if the holder dies.

Keeping these in one place means the crash-safety and concurrency guarantees are written
and tested once, not re-derived (and subtly broken) per store.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from types import TracebackType

try:  # POSIX advisory locking (macOS + Linux — the stated deploy targets).
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX fallback
    _HAVE_FCNTL = False


class FileLock:
    """A POSIX ``flock`` advisory lock scoped to a ``with`` block.

    Degrades to a no-op where ``fcntl`` is unavailable; the atomic ``os.replace`` in
    :func:`atomic_write` still guarantees readers never see a torn file, so the worst
    case without real locking is a last-writer-wins race between two concurrent writers
    — acceptable for a single user's tooling, and not a concern on the supported
    platforms. (Windows support via ``filelock`` is a separate, planned change.)
    """

    def __init__(self, path: Path, *, exclusive: bool = True) -> None:
        self._path = path
        self._exclusive = exclusive
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        if _HAVE_FCNTL:
            fcntl.flock(self._fd, fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


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
