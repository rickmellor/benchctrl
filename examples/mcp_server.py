"""Start the OpenSMU MCP server.

Equivalent to the `opensmu-mcp` console script that ships with the
package; this file exists as a self-documenting launcher and as a
template for users who want to embed the server in a larger Python
program.

The MCP (Model Context Protocol) server exposes the entire OpenSMU
SDK as 93 tools that any MCP-aware client can call: Claude Code,
Claude Desktop, etc. Every public SDK method has a matching tool.

Setup:
    pip install opensmu[mcp]

Run:
    python examples/mcp_server.py
    # or equivalently:
    opensmu-mcp
    python -m opensmu.mcp

In your MCP client's settings, add a server pointing at one of those
entries. Concrete examples in docs/mcp.md.

Safety notes:
- The server holds the SMU connection across tool calls. Only one
  process can hold the device at a time.
- Tools that drive voltage onto the output (`enable_output`,
  `dl3031a_set_input`) require explicit confirmation arguments —
  the LLM has to opt into making the bench live.
- The server assumes single-client serialization; concurrent clients
  against the same server is unsupported.
"""

from __future__ import annotations

from opensmu.mcp import main


if __name__ == "__main__":
    main()
