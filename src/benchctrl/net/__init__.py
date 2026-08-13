"""Shared client/agent networking. Stdlib only.

Imported by both ends of the remote link, so nothing here may depend on
anything the board cannot install: the agent runs on a Debian image with
only ``pyserial`` present, and the MCP stack's compiled dependencies are
unavailable for that architecture.
"""

from __future__ import annotations

__all__ = ["frames", "codec", "errors", "auth", "client", "proxy", "beacon"]
