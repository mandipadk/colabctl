"""Credential diagnostics — introspect an access token via Google's tokeninfo endpoint.

Powers ``colabctl auth status``: shows the authenticated account, the granted scopes
(so a missing ``colaboratory``/``drive.file`` is caught up front instead of as a runtime
403/401), and the token's client/expiry. No SDK needed — a single HTTPS GET.
"""

from __future__ import annotations

from typing import Any

import httpx

from colabctl.errors import AuthError

_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

COLABORATORY_SCOPE = "https://www.googleapis.com/auth/colaboratory"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


async def token_info(token: str, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Return Google's tokeninfo for ``token`` (``scope``, ``email``, ``expires_in``, ``azp``)."""
    owns = http is None
    client = http or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(_TOKENINFO_URL, params={"access_token": token})
    finally:
        if owns:
            await client.aclose()
    if resp.status_code != 200:
        raise AuthError(f"tokeninfo failed: HTTP {resp.status_code} {resp.text[:200]!r}")
    data: dict[str, Any] = resp.json()
    return data


def scopes_of(info: dict[str, Any]) -> set[str]:
    """The set of scopes from a tokeninfo response."""
    return set(str(info.get("scope", "")).split())


__all__ = [
    "COLABORATORY_SCOPE",
    "DRIVE_FILE_SCOPE",
    "scopes_of",
    "token_info",
]
