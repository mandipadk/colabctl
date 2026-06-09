"""Adversarial + property-based stress tests for the secret stores."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from colabctl.errors import SecretStoreError
from colabctl.secrets import EncryptedFileSecretStore, KeyringSecretStore, MemorySecretStore


class FakeKeyring:
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


# --- the bug this suite was written to catch -------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "colabctl-chunked:3",  # looks exactly like the manifest marker
        "colabctl-chunked:notanint",  # would crash int() on the old code
        "colabctl-raw:already-prefixed",
        "colabctl-chunked:",
    ],
)
def test_keyring_values_that_collide_with_markers(value):
    store = KeyringSecretStore(FakeKeyring(), chunk_size=8)
    store.set("acct", value)
    assert store.get("acct") == value  # must round-trip verbatim, never mis-parse


@pytest.mark.parametrize("size", [0, 1, 7, 8, 9, 16, 17, 100])
def test_keyring_chunk_boundaries(size):
    store = KeyringSecretStore(FakeKeyring(), chunk_size=8)
    value = "x" * size
    store.set("acct", value)
    assert store.get("acct") == value


def test_keyring_unicode_and_multibyte():
    store = KeyringSecretStore(FakeKeyring(), chunk_size=4)
    value = "🔑—token—значение—🚀" * 5
    store.set("a@x.com", value)
    assert store.get("a@x.com") == value


def test_keyring_overwrite_large_then_small_then_large():
    backend = FakeKeyring()
    store = KeyringSecretStore(backend, chunk_size=4)
    store.set("k", "0123456789")  # chunked
    store.set("k", "tiny")  # single
    assert store.get("k") == "tiny"
    assert all("#" not in u for (_, u) in backend.store)  # no orphan chunks
    store.set("k", "9876543210")  # chunked again
    assert store.get("k") == "9876543210"


def test_keyring_account_names_do_not_collide():
    store = KeyringSecretStore(FakeKeyring(), chunk_size=4)
    store.set("acct", "AAAAAAAA")  # chunks acct#0, acct#1
    store.set("acct#0", "separate")  # an account literally named like a chunk
    assert store.get("acct") == "AAAAAAAA"
    assert store.get("acct#0") == "separate"


@settings(max_examples=200)
@given(value=st.text(), chunk=st.integers(min_value=1, max_value=16))
def test_keyring_roundtrip_property(value, chunk):
    store = KeyringSecretStore(FakeKeyring(), chunk_size=chunk)
    store.set("k", value)
    assert store.get("k") == value


# --- encrypted file --------------------------------------------------------


def test_encrypted_file_corrupt_file_raises(tmp_path):
    path = tmp_path / "s.enc"
    path.write_text("this is not valid json {{{")
    with pytest.raises(SecretStoreError):
        EncryptedFileSecretStore(path=path, passphrase="pw").get("k")


def test_encrypted_file_empty_file_raises(tmp_path):
    path = tmp_path / "s.enc"
    path.write_text("")
    with pytest.raises(SecretStoreError):
        EncryptedFileSecretStore(path=path, passphrase="pw").get("k")


def test_encrypted_file_unicode_large_roundtrip(tmp_path):
    store = EncryptedFileSecretStore(path=tmp_path / "s.enc", passphrase="pw")
    blob = "🔐 valɣe with\nnewlines\tand tabs " * 2000
    store.set("k", blob)
    assert EncryptedFileSecretStore(path=tmp_path / "s.enc", passphrase="pw").get("k") == blob


def test_encrypted_file_delete_nonexistent_is_noop(tmp_path):
    store = EncryptedFileSecretStore(path=tmp_path / "s.enc", passphrase="pw")
    store.delete("missing")  # no error, no file needed
    store.set("k", "v")
    store.delete("missing")  # still no error


def test_encrypted_file_get_missing_returns_none(tmp_path):
    store = EncryptedFileSecretStore(path=tmp_path / "s.enc", passphrase="pw")
    store.set("a", "1")
    assert store.get("b") is None


def test_encrypted_file_service_isolation(tmp_path):
    store = EncryptedFileSecretStore(path=tmp_path / "s.enc", passphrase="pw")
    store.set("acct", "in-default", service="svc-a")
    store.set("acct", "in-other", service="svc-b")
    assert store.get("acct", service="svc-a") == "in-default"
    assert store.get("acct", service="svc-b") == "in-other"
    assert set(store.list_accounts(service="svc-a")) == {"acct"}


# --- memory ----------------------------------------------------------------


def test_memory_service_isolation_and_list():
    store = MemorySecretStore()
    store.set("a", "1", service="s1")
    store.set("a", "2", service="s2")
    assert store.get("a", service="s1") == "1"
    assert store.list_accounts(service="s1") == ["a"]
    assert store.list_accounts(service="s2") == ["a"]
