"""Crash-safety + locking for the shared fs primitives and the encrypted secret store.

Covers the Phase-0.4 fix: ``EncryptedFileSecretStore`` writes must be atomic (a crash
mid-write cannot corrupt the existing store) and lock-guarded, on the exact headless/CI
path the store exists for.
"""

from __future__ import annotations

import os

import pytest

from colabctl import fsutil
from colabctl.fsutil import FileLock, atomic_write
from colabctl.secrets import encrypted_file
from colabctl.secrets.encrypted_file import EncryptedFileSecretStore

_POSIX = os.name == "posix"


def test_atomic_write_roundtrip_and_perms(tmp_path):
    p = tmp_path / "nested" / "doc.txt"
    atomic_write(p, "hello")
    assert p.read_text() == "hello"
    if _POSIX:
        assert oct(p.stat().st_mode & 0o777) == "0o600"
        assert oct(p.parent.stat().st_mode & 0o777) == "0o700"


def test_atomic_write_failure_leaves_prior_file_intact(tmp_path, monkeypatch):
    p = tmp_path / "doc.txt"
    atomic_write(p, "v1")

    # Simulate a crash partway through the write (after the temp file exists).
    def boom(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(fsutil.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated fsync failure"):
        atomic_write(p, "v2")

    # The original content survives, and no temp turds are left behind.
    assert p.read_text() == "v1"
    leftovers = [q.name for q in tmp_path.iterdir() if q.name.startswith(".tmp-")]
    assert leftovers == []


def test_filelock_acquire_release(tmp_path):
    lock = tmp_path / "x.lock"
    with FileLock(lock, exclusive=True):
        assert lock.exists()
    # A second acquisition after release must succeed (no stale lock).
    with FileLock(lock, exclusive=True):
        pass


def test_filelock_blocks_across_processes(tmp_path):
    # The Windows-safety value: the lock is held against a *separate process*, not just no-op'd.
    import subprocess
    import sys

    from filelock import Timeout

    lock = tmp_path / "x.lock"
    child = (
        "import time; from filelock import FileLock; "
        f"_l = FileLock({str(lock)!r}); _l.acquire(); print('held', flush=True); time.sleep(10)"
    )
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"  # the child now holds the lock
        with pytest.raises(Timeout):  # we cannot acquire it while the child holds it
            with FileLock(lock, timeout=0.4):
                pass
    finally:
        proc.terminate()
        proc.wait()


def _store(tmp_path):
    return EncryptedFileSecretStore(path=tmp_path / "secrets.enc", passphrase="pw")


def test_encrypted_store_perms(tmp_path):
    store = _store(tmp_path)
    store.set("a", "1")
    if _POSIX:
        assert oct((tmp_path / "secrets.enc").stat().st_mode & 0o777) == "0o600"
        assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"


def test_encrypted_store_crash_during_save_preserves_existing_secrets(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.set("keep", "v1")

    # A crash during the next save must not corrupt the existing encrypted file.
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(encrypted_file, "atomic_write", boom)
    with pytest.raises(OSError, match="disk full"):
        store.set("new", "v2")

    # Reopen from disk: the prior secret is intact; the failed write left no trace.
    reopened = _store(tmp_path)
    assert reopened.get("keep") == "v1"
    assert reopened.get("new") is None


def test_encrypted_store_set_preserves_other_keys(tmp_path):
    store = _store(tmp_path)
    for i in range(10):
        store.set(f"k{i}", f"v{i}")
    store.delete("k5")
    reopened = _store(tmp_path)
    assert reopened.get("k0") == "v0"
    assert reopened.get("k9") == "v9"
    assert reopened.get("k5") is None
    assert set(reopened.list_accounts()) == {f"k{i}" for i in range(10)} - {"k5"}
