"""The sanctioned-default transport: a thin, version-pinned wrapper around the
official ``google-colab-cli`` (``colab``) invoked as an isolated subprocess.

Because the CLI has **no machine-readable output mode** (verified in Phase 0), the
:mod:`~colabctl.transport.cli.parser` module owns a tolerant, golden-tested parser
for the CLI's human stdout grammar, pinned to a known CLI version. The
:class:`~colabctl.transport.cli.adapter.ColabCliTransport` composes that parser
with async subprocess invocation behind the standard ``TransportAdapter``.
"""

from __future__ import annotations

from colabctl.transport.cli.adapter import ColabCliTransport

__all__ = ["ColabCliTransport"]
