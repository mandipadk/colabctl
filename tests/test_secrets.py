"""Tests for the secret stores: memory, keyring (with chunking), encrypted-file."""

from __future__ import annotations

import pytest

from colabctl.errors import SecretStoreError
from colabctl.secrets import (
    EncryptedFileSecretStore,
    KeyringSecretStore,
    MemorySecretStore,
)
from colabctl.secrets.base import join_chunks, split_chunks

# --- helpers ----------------------------------------------------------------


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module API."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise KeyError("not found")
        del self.store[(service, username)]


# --- chunk helpers ----------------------------------------------------------


def test_split_and_join_chunks():
    assert split_chunks("abcdefg", 3) == ["abc", "def", "g"]
    assert split_chunks("", 3) == [""]
    assert join_chunks(split_chunks("hello world", 4)) == "hello world"


def test_split_chunks_rejects_bad_size():
    with pytest.raises(ValueError):
        split_chunks("x", 0)


# --- memory store -----------------------------------------------------------


def test_memory_store_roundtrip():
    store = MemorySecretStore()
    assert store.get("a@x.com") is None
    store.set("a@x.com", "secret")
    assert store.get("a@x.com") == "secret"
    assert store.list_accounts() == ["a@x.com"]
    store.delete("a@x.com")
    assert store.get("a@x.com") is None
    store.delete("a@x.com")  # idempotent


# --- keyring store ----------------------------------------------------------


def test_keyring_small_value_single_item_roundtrip():
    backend = FakeKeyring()
    store = KeyringSecretStore(backend, chunk_size=64)
    store.set("acct", "short")
    # stored as one (prefixed) item, not chunked
    assert backend.store[("colabctl", "acct")] == "colabctl-raw:short"
    assert all("#" not in username for (_, username) in backend.store)
    assert store.get("acct") == "short"


def test_keyring_large_value_is_chunked_and_reassembled():
    backend = FakeKeyring()
    store = KeyringSecretStore(backend, chunk_size=4)
    value = "0123456789"  # 10 chars → 3 chunks of size 4
    store.set("acct", value)
    # Manifest at the account; chunks at sentinel-namespaced keys.
    assert backend.store[("colabctl", "acct")] == "colabctl-chunked:3"
    assert backend.store[("colabctl", KeyringSecretStore._chunk_name("acct", 0))] == "0123"
    assert backend.store[("colabctl", KeyringSecretStore._chunk_name("acct", 2))] == "89"
    assert store.get("acct") == value


def test_keyring_delete_cleans_chunks():
    backend = FakeKeyring()
    store = KeyringSecretStore(backend, chunk_size=4)
    store.set("acct", "0123456789")
    store.delete("acct")
    assert backend.store == {}
    assert store.get("acct") is None


def test_keyring_overwrite_chunked_with_small_value():
    backend = FakeKeyring()
    store = KeyringSecretStore(backend, chunk_size=4)
    store.set("acct", "0123456789")
    store.set("acct", "tiny")
    assert store.get("acct") == "tiny"
    # No orphaned chunk items remain.
    assert all("#" not in username for (_, username) in backend.store)


# --- encrypted-file store ---------------------------------------------------


def test_encrypted_file_roundtrip_and_persistence(tmp_path):
    path = tmp_path / "secrets.enc"
    store = EncryptedFileSecretStore(path=path, passphrase="hunter2")
    store.set("a@x.com", "token-value")
    store.set("oauth:adc", "blob" * 1000)  # large value
    assert path.exists()

    # A fresh instance with the same passphrase reads the same data.
    reopened = EncryptedFileSecretStore(path=path, passphrase="hunter2")
    assert reopened.get("a@x.com") == "token-value"
    assert reopened.get("oauth:adc") == "blob" * 1000
    assert set(reopened.list_accounts()) == {"a@x.com", "oauth:adc"}

    reopened.delete("a@x.com")
    assert reopened.get("a@x.com") is None


def test_encrypted_file_wrong_passphrase_fails(tmp_path):
    path = tmp_path / "secrets.enc"
    EncryptedFileSecretStore(path=path, passphrase="right").set("k", "v")
    bad = EncryptedFileSecretStore(path=path, passphrase="wrong")
    with pytest.raises(SecretStoreError):
        bad.get("k")


def test_encrypted_file_requires_passphrase(monkeypatch, tmp_path):
    monkeypatch.delenv("COLABCTL_SECRET_PASSPHRASE", raising=False)
    with pytest.raises(SecretStoreError):
        EncryptedFileSecretStore(path=tmp_path / "s.enc")
