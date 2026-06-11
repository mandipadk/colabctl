"""Browser-bridge transport over Colab's "local MCP server" (ColabMCP).

The sanctioned, human-in-the-loop Colab path: a local origin-restricted, token-authed
WebSocket that a logged-in Colab tab connects to as an MCP **server**, exposing Colab's
own notebook tools (``run_code_cell``, ``add_code_cell``, ``get_cells``, …). colabctl is
the MCP **client** (:class:`McpClient`) and drives those tools to execute code, move files,
and — uniquely among the transports — **keep the runtime alive** (a no-op cell is genuine
activity in the authenticated session). Protocol confirmed live in Phase A (2026-06-11);
see ``spikes/PHASE-A-FINDINGS.md`` ⑤/⑧.
"""

from __future__ import annotations

from colabctl.transport.browser.bridge import BrowserBridgeTransport
from colabctl.transport.browser.mcp import McpClient, mcp_text

__all__ = ["BrowserBridgeTransport", "McpClient", "mcp_text"]
