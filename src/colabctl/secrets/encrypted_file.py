"""Encrypted-file secret store for headless Linux / CI (no OS keychain).

Secrets are stored in a single file encrypted with Fernet (AES-128-CBC + HMAC),
keyed by a scrypt-derived key from a passphrase (constructor arg or the
``COLABCTL_SECRET_PASSPHRASE`` env var). The file is created ``0600``. This is the
``both``-deployment path chosen for Phase 1: desktops use the keychain, servers/CI
use this. ``cryptography`` is imported lazily so the core stays light.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from colabctl.errors import SecretStoreError
from colabctl.secrets.base import DEFAULT_SERVICE, SecretStore

_ENV_PASSPHRASE = "COLABCTL_SECRET_PASSPHRASE"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32


def _default_path() -> Path:
    return Path("~/.config/colabctl/secrets.enc").expanduser()


class EncryptedFileSecretStore(SecretStore):
    """A passphrase-encrypted, file-backed secret store.

    Args:
        path: file location (default ``~/.config/colabctl/secrets.enc``).
        passphrase: encryption passphrase; falls back to ``$COLABCTL_SECRET_PASSPHRASE``.
    """

    def __init__(self, *, path: Path | None = None, passphrase: str | None = None) -> None:
        self._path = path or _default_path()
        pw = passphrase if passphrase is not None else os.environ.get(_ENV_PASSPHRASE)
        if not pw:
            raise SecretStoreError(
                "No passphrase provided. Pass passphrase=... or set "
                f"${_ENV_PASSPHRASE} to use the encrypted-file secret store."
            )
        self._passphrase = pw.encode()

    # -- public API ---------------------------------------------------------

    def get(self, account: str, *, service: str = DEFAULT_SERVICE) -> str | None:
        return self._load().get(self._key(service, account))

    def set(self, account: str, value: str, *, service: str = DEFAULT_SERVICE) -> None:
        data = self._load()
        data[self._key(service, account)] = value
        self._save(data)

    def delete(self, account: str, *, service: str = DEFAULT_SERVICE) -> None:
        data = self._load()
        if data.pop(self._key(service, account), None) is not None:
            self._save(data)

    def list_accounts(self, *, service: str = DEFAULT_SERVICE) -> list[str]:
        prefix = f"{service}\x00"
        return [k[len(prefix) :] for k in self._load() if k.startswith(prefix)]

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _key(service: str, account: str) -> str:
        return f"{service}\x00{account}"

    def _fernet(self, salt: bytes) -> Any:
        try:
            from cryptography.fernet import Fernet
            from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise SecretStoreError(
                "cryptography is not installed. Install with `pip install 'colabctl[secrets]'`."
            ) from exc
        kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        key = base64.urlsafe_b64encode(kdf.derive(self._passphrase))
        return Fernet(key)

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            envelope = json.loads(self._path.read_text())
            salt = base64.b64decode(envelope["salt"])
            token = envelope["data"].encode()
            plaintext = self._fernet(salt).decrypt(token)
            result: dict[str, str] = json.loads(plaintext)
            return result
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(
                f"Could not read secret store at {self._path} (corrupt file or wrong passphrase)."
            ) from exc

    def _save(self, data: dict[str, str]) -> None:
        salt = self._existing_salt() or os.urandom(16)
        token = self._fernet(salt).encrypt(json.dumps(data).encode())
        envelope = {
            "v": 1,
            "salt": base64.b64encode(salt).decode(),
            "data": token.decode(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(envelope))
        self._path.chmod(0o600)

    def _existing_salt(self) -> bytes | None:
        if not self._path.exists():
            return None
        try:
            return base64.b64decode(json.loads(self._path.read_text())["salt"])
        except Exception:
            return None
