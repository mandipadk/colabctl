"""Authentication contract.

An :class:`AuthProvider` yields a fresh OAuth bearer token (with the Colab scopes)
on demand. Transports depend only on this; concrete providers (ADC, OAuth2-loopback,
static) are swappable. The Colab scope set is the one verified in Phase 0 — note
that ``colaboratory`` is not third-party-grantable, so ADC (gcloud's client) is the
working path; ``cloud-platform`` is additionally required by gcloud itself.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable

#: OAuth scopes the Colab backend requires (verified from CLI ``PUBLIC_SCOPES``).
COLAB_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
)

#: ADC via ``gcloud`` additionally requires these (gcloud refuses otherwise).
ADC_LOGIN_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
)

TokenCallable = Callable[[], Awaitable[str]]


class AuthProvider(abc.ABC):
    """Yields fresh bearer tokens for the Colab backend."""

    @abc.abstractmethod
    async def token(self) -> str:
        """Return a currently-valid bearer token, refreshing if needed."""

    async def email(self) -> str | None:
        """Return the authenticated account email if known (best-effort)."""
        return None

    def as_token_callable(self) -> TokenCallable:
        """Adapt to the ``TokenProvider`` callable the native client expects."""
        return self.token


class StaticTokenProvider(AuthProvider):
    """An :class:`AuthProvider` wrapping a fixed token — for tests/injection."""

    def __init__(self, token: str, *, email: str | None = None) -> None:
        self._token = token
        self._email = email

    async def token(self) -> str:
        return self._token

    async def email(self) -> str | None:
        return self._email
