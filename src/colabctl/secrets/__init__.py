"""Secret storage: pluggable backends behind one :class:`SecretStore` contract.

- :class:`KeyringSecretStore` — OS keychain (desktops).
- :class:`EncryptedFileSecretStore` — passphrase-encrypted file (headless/CI).
- :class:`MemorySecretStore` — ephemeral, for tests.

:func:`default_secret_store` picks the right one for the environment.
"""

from __future__ import annotations

import os

from colabctl.secrets.base import DEFAULT_SERVICE, SecretStore
from colabctl.secrets.encrypted_file import EncryptedFileSecretStore
from colabctl.secrets.keyring_store import KeyringSecretStore
from colabctl.secrets.memory import MemorySecretStore

__all__ = [
    "DEFAULT_SERVICE",
    "EncryptedFileSecretStore",
    "KeyringSecretStore",
    "MemorySecretStore",
    "SecretStore",
    "default_secret_store",
]


def default_secret_store() -> SecretStore:
    """Select a secret store for the current environment.

    Precedence: an explicit ``$COLABCTL_SECRET_PASSPHRASE`` forces the encrypted
    file store (the headless/CI path); otherwise the OS keychain is used.
    """
    if os.environ.get("COLABCTL_SECRET_PASSPHRASE"):
        return EncryptedFileSecretStore()
    return KeyringSecretStore()
