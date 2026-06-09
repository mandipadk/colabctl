"""Secret-store contract + helpers.

colabctl never writes credentials to plaintext on disk. Secrets live in an OS
keychain (defense-in-depth, not a trust boundary) on desktops, and in an
encrypted file (passphrase from env) on headless Linux/CI — both behind this one
interface so the rest of the code is storage-agnostic. The chunking helpers exist
because keyring backends (macOS Keychain) have a ~4 KB soft limit per item, and
OAuth blobs can exceed it.
"""

from __future__ import annotations

import abc

#: Single keychain "service"; individual secrets are addressed by ``account``
#: (e.g. an email, or a logical key like ``"oauth:adc"``).
DEFAULT_SERVICE = "colabctl"

#: Conservative per-item chunk size (keyring/Keychain soft-limit is ~4 KB).
DEFAULT_CHUNK_SIZE = 2048


class SecretStore(abc.ABC):
    """Abstract credential store. Implementations must be process-safe enough for
    a single user's interactive + daemon use; they are not concurrency-hardened
    multi-writer stores."""

    @abc.abstractmethod
    def get(self, account: str, *, service: str = DEFAULT_SERVICE) -> str | None:
        """Return the secret for ``account`` or ``None`` if absent."""

    @abc.abstractmethod
    def set(self, account: str, value: str, *, service: str = DEFAULT_SERVICE) -> None:
        """Store ``value`` for ``account`` (overwriting any existing value)."""

    @abc.abstractmethod
    def delete(self, account: str, *, service: str = DEFAULT_SERVICE) -> None:
        """Remove ``account``'s secret. No error if it does not exist."""

    def list_accounts(self, *, service: str = DEFAULT_SERVICE) -> list[str]:
        """List known accounts. Optional; not all backends can enumerate."""
        raise NotImplementedError(f"{type(self).__name__} cannot list accounts")


def split_chunks(value: str, size: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """Split ``value`` into ``size``-char chunks (always at least one chunk)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if not value:
        return [""]
    return [value[i : i + size] for i in range(0, len(value), size)]


def join_chunks(chunks: list[str]) -> str:
    """Inverse of :func:`split_chunks`."""
    return "".join(chunks)
