"""Union node: the client side of Union.

* `config`     - where a node keeps its identity and settings (`<project>/.union/`)
* `client`     - signed HTTP and the event stream against the Union server
* `node`       - the runtime: join, rotate, receive, send, spool for the harness
* `mcp_server` - the MCP tools an AI harness calls
* `cli`        - `union join | status | agents | mode | send | listen | mcp | tail`
"""
__version__ = "0.1.0"
