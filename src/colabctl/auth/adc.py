"""ADC auth provider — the Phase 0-verified working path.

Uses Application Default Credentials (``gcloud auth application-default login
--scopes=…colaboratory``). ``google.auth`` is sync, so refreshes run in a thread;
a lock serializes concurrent refreshes. ``google-auth`` is imported lazily.

Setup (once, by the user):

    gcloud auth application-default login \\
        --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\\
    https://www.googleapis.com/auth/userinfo.email,\\
    https://www.googleapis.com/auth/colaboratory,\\
    https://www.googleapis.com/auth/drive.file
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any

from colabctl.auth.base import COLAB_SCOPES, AuthProvider
from colabctl.errors import AuthError


class ADCAuthProvider(AuthProvider):
    """Bearer tokens from Application Default Credentials with the Colab scopes."""

    def __init__(self, *, scopes: tuple[str, ...] = COLAB_SCOPES) -> None:
        self._scopes = list(scopes)
        self._creds: Any | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._sync_token)

    async def email(self) -> str | None:
        async with self._lock:
            creds = self._creds
        # ADC user creds expose the signer email for service accounts; user creds
        # usually don't, so this is best-effort.
        return getattr(creds, "service_account_email", None) if creds else None

    @property
    def quota_project_id(self) -> str | None:
        """The ADC quota project (``gcloud auth application-default set-quota-project``).

        Known once credentials have been loaded (after the first ``token()``); ``None``
        if no quota project is bound — in which case Drive API calls will 403.
        """
        return getattr(self._creds, "quota_project_id", None)

    def _sync_token(self) -> str:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise AuthError(
                "google-auth is not installed. Install with `pip install 'colabctl[native]'`."
            ) from exc

        # Typed Any: google-auth credential subclasses differ structurally
        # (only scopable creds have with_scopes; refresh is untyped upstream).
        creds: Any = self._creds
        if creds is None:
            # ADC user creds emit a noisy "no quota project" UserWarning on every
            # call; it's irrelevant here (Colab calls pin their own project), so
            # suppress just that one message.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Your application has authenticated using end user credentials.*",
                    category=UserWarning,
                )
                creds, _ = google.auth.default(scopes=self._scopes)
        # User creds ignore scopes= in default(); re-apply when supported.
        if getattr(creds, "requires_scopes", False):
            creds = creds.with_scopes(self._scopes)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AuthError(f"Failed to refresh ADC credentials: {exc}") from exc
        self._creds = creds
        token = getattr(creds, "token", None)
        if not token:
            raise AuthError("ADC credentials produced no access token.")
        return str(token)
