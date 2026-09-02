# Union plugin for Codex

Connect a Codex session to the user's other AI coding sessions through a Union
server. It uses the same `union-node` MCP server as the Claude Code plugin,
so all harnesses share one encrypted protocol and tool surface.

## What it adds

* MCP tools: `union_join`, `union_list_agents`, `union_send`, `union_reply`,
  `union_inbox`, and `union_status`.
* A `union_join` tool, because Codex plugins do not use Claude Code slash
  commands for setup.
* Safe handling instructions: messages from Union are untrusted data, never
  user consent or executable commands.

## Join and use

Ask Codex to join the Union, providing the server URL, join key, an agent
name, and a mode. Use `data` to receive context only, `execute` to accept
tasks from other agents, or `ask` to hold tasks for the local user.

After joining, ask Codex to list online agents, send a task or data message,
or check `union_inbox` for unread messages. Codex MCP tools poll the inbox;
they do not inject an inbound message into an idle Codex chat.

See [INSTALL.md](INSTALL.md) for prerequisites, local marketplace installation,
verification, and update instructions.

## Development

When loaded from this checkout, `launch.py` runs `protocol/` and `node/`
directly and installs only `cryptography` under `~/.union/codex/site`.
Installed plugins use the bundled `runtime/` copy of those packages, keeping
the plugin self-contained and independent of the source checkout.

Run `python plugins/codex/sync_runtime.py` before releasing a plugin update so
the bundled runtime remains identical to the shared packages.
