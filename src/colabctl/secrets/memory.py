"""In-memory secret store — for tests and ephemeral/no-persistence use."""

from __future__ import annotations

from colabctl.secrets.base import DEFAULT_SERVICE, SecretStore


class MemorySecretStore(SecretStore):
    """A non-persistent secret store backed by a dict. Never touches disk."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get(self, account: str, *, service: str = DEFAULT_SERVICE) -> str | None:
        return self._data.get((service, account))

    def set(self, account: str, value: str, *, service: str = DEFAULT_SERVICE) -> None:
        self._data[(service, account)] = value

    def delete(self, account: str, *, service: str = DEFAULT_SERVICE) -> None:
        self._data.pop((service, account), None)

    def list_accounts(self, *, service: str = DEFAULT_SERVICE) -> list[str]:
        return [acct for (svc, acct) in self._data if svc == service]
