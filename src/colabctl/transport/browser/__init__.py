"""Browser-bridge transport (colab-mcp model) — sanctioned, human-in-the-loop.

This is the secondary Colab path from the spec: a local, origin-restricted,
token-authed WebSocket relay opens a logged-in Colab tab; the Colab frontend JS
connects back and performs the privileged backend work, while we relay
:class:`TransportAdapter` operations as JSON-RPC. It is **not headless** (needs an
open browser tab) — the CLI/native transports cover the automated case.

Status: structurally complete and unit-tested at the JSON-RPC relay layer, but the
end-to-end flow depends on Google's colab-mcp frontend protocol and **is not
live-validated** (the request/result shapes here follow the documented colab-mcp
model and must be confirmed against the live frontend).
"""

from __future__ import annotations

from colabctl.transport.browser.bridge import BrowserBridgeTransport

__all__ = ["BrowserBridgeTransport"]
