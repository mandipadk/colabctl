"""OS-keychain secret store via ``keyring``, with >4 KB chunking.

The low-level keychain backend is injectable (defaults to the ``keyring`` module),
which keeps the chunking logic unit-testable without a real keychain. Large
secrets are split across ``account#0…#n-1`` items with a small manifest stored at
``account`` — transparent to callers.
"""

from __future__ import annotations

import contextlib
from typing import Protocol, cast

from colabctl.errors import SecretStoreError
from colabctl.secrets.base import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SERVICE,
    SecretStore,
    join_chunks,
    split_chunks,
)

# A single-chunk value is stored under _RAW_PREFIX and a multi-chunk value under
# _MANIFEST_PREFIX (+ chunks). Prefixing BOTH means a user value that happens to look
# like a manifest (e.g. literally "colabctl-chunked:3") can never be mis-parsed.
_MANIFEST_PREFIX = "colabctl-chunked:"
_RAW_PREFIX = "colabctl-raw:"
# Chunk keys are namespaced with a NUL-delimited sentinel so a chunk key can never
# collide with a real account name (e.g. account "acct#0" vs the chunks of "acct").
_CHUNK_PREFIX = "\x00colabctl-chunk\x00"
_CHUNK_SEP = "#"


class KeyringBackend(Protocol):
    """The subset of the ``keyring`` module API we depend on."""

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def _load_keyring() -> KeyringBackend:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SecretStoreError(
            "keyring is not installed. Install with `pip install 'colabctl[secrets]'` "
            "or use EncryptedFileSecretStore on headless hosts."
        ) from exc
    return cast(KeyringBackend, keyring)


class KeyringSecretStore(SecretStore):
    """Secret store backed by the OS keychain.

    Args:
        backend: low-level keychain API (defaults to the ``keyring`` module).
        chunk_size: max chars per keychain item before chunking kicks in.
    """

    def __init__(
        self,
        backend: KeyringBackend | None = None,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._backend = backend if backend is not None else _load_keyring()
        self._chunk_size = chunk_size

    def get(self, account: str, *, service: str = DEFAULT_SERVICE) -> str | None:
        raw = self._backend.get_password(service, account)
        if raw is None:
            return None
        if raw.startswith(_MANIFEST_PREFIX):
            count = int(raw[len(_MANIFEST_PREFIX) :])  # safe: we only ever write digits here
            chunks: list[str] = []
            for i in range(count):
                part = self._backend.get_password(service, self._chunk_name(account, i))
                if part is None:
                    raise SecretStoreError(
                        f"Chunked secret {account!r} is missing chunk {i}/{count}."
                    )
                chunks.append(part)
            return join_chunks(chunks)
        if raw.startswith(_RAW_PREFIX):
            return raw[len(_RAW_PREFIX) :]
        return raw  # value written outside this store (no prefix)

    def set(self, account: str, value: str, *, service: str = DEFAULT_SERVICE) -> None:
        # Clear any prior (possibly chunked) value first so we never orphan chunks.
        self.delete(account, service=service)
        chunks = split_chunks(value, self._chunk_size)
        if len(chunks) == 1:
            self._backend.set_password(service, account, _RAW_PREFIX + value)
            return
        for i, chunk in enumerate(chunks):
            self._backend.set_password(service, self._chunk_name(account, i), chunk)
        self._backend.set_password(service, account, f"{_MANIFEST_PREFIX}{len(chunks)}")

    def delete(self, account: str, *, service: str = DEFAULT_SERVICE) -> None:
        raw = self._backend.get_password(service, account)
        if raw is not None and raw.startswith(_MANIFEST_PREFIX):
            count = int(raw[len(_MANIFEST_PREFIX) :])
            for i in range(count):
                self._safe_delete(service, self._chunk_name(account, i))
        self._safe_delete(service, account)

    def _safe_delete(self, service: str, username: str) -> None:
        # Idempotent delete: absence is not an error (keyring raises a
        # backend-specific "password not found").
        with contextlib.suppress(Exception):
            self._backend.delete_password(service, username)

    @staticmethod
    def _chunk_name(account: str, index: int) -> str:
        return f"{_CHUNK_PREFIX}{account}{_CHUNK_SEP}{index}"
