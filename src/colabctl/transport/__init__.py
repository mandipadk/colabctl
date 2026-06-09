"""Transport layer: pluggable ways to reach a Colab runtime.

A *transport* is the lowest layer that knows how to allocate a runtime, run
code on it, move files, and tear it down. Higher layers (provider abstraction,
SDK, CLI, MCP) depend only on the :class:`~colabctl.transport.base.TransportAdapter`
contract, never on a concrete transport.

Concrete transports:
- ``cli``    — wraps the official ``google-colab-cli`` (sanctioned default).
- ``native`` — our from-scratch ``/tun/m/*`` + Jupyter-websocket client (co-primary, opt-in).
- ``browser``— colab-mcp browser-bridge (secondary, human-in-the-loop) [planned].
"""

from __future__ import annotations

from colabctl.transport.base import Capabilities, TransportAdapter

__all__ = ["Capabilities", "TransportAdapter"]
