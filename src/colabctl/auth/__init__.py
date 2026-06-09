"""Authentication providers for the Colab backend."""

from __future__ import annotations

from colabctl.auth.adc import ADCAuthProvider
from colabctl.auth.base import (
    ADC_LOGIN_SCOPES,
    COLAB_SCOPES,
    AuthProvider,
    StaticTokenProvider,
    TokenCallable,
)

__all__ = [
    "ADC_LOGIN_SCOPES",
    "COLAB_SCOPES",
    "ADCAuthProvider",
    "AuthProvider",
    "StaticTokenProvider",
    "TokenCallable",
]
