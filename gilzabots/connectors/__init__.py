"""MCP connectors: external Model Context Protocol servers the agent can use.

A *connector* is the user's registration of one MCP server -- a Streamable HTTP
endpoint or a local command spoken to over stdio -- plus how to authenticate to it
and which of its tools the agent may hold. Connectors are global (one registry per
GilzaBots home) and their tools reach the main chat agent as ordinary tools, every
call passing the same permission gate as the native tools (PROJECT_DESIGN 2.25).

Modules:

* `store`       -- the connector registry (``<gilzabots home>/mcp_connectors.json``).
* `credentials` -- OAuth tokens and client registrations, one file per connector.
* `oauth`       -- the MCP authorization flow through the system browser.
* `manager`     -- live connections, opened lazily and owned by one event loop.
* `agent_tools` -- the connectors' tools as agent tools, behind the permission gate.
* `api`         -- the ``/api/connectors/*`` endpoints of the desktop server.
"""
