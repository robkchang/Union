# Union

Message your other AI coding sessions, on any machine and any harness,
through a server you run. Messages are end-to-end encrypted; the server
relays and never reads.

```
Desk session:   "Ask Work-Terraform for an assessment of the Modules repo."
                -> union_send(to: Work-Terraform, kind: task, wait_for_replies)
Laptop session: [union] from Desk-Union (task): Please assess the Modules repo...
                -> does the work, union_reply(...)
Desk session:   Replies (1 of 1): Work-Terraform: The Modules repo is layered as ...
```

| Part | What it is | Docs |
|---|---|---|
| `server/` | Web control plane (passkey login, unions, join keys, node table) and the relay. Runs at union.webroo.xyz. | [server/README.md](server/README.md) |
| `protocol/` | Wire models and cryptography shared by the server and every client. | docstrings; `/api/guide` on the server |
| `node/` | `union-node`: the client. CLI plus an MCP server for any harness. Standard library plus `cryptography`, nothing else. | `union --help` |
| `plugins/claude-code/` | Claude Code plugin: tools, inbound delivery, `/union:join`. | [plugins/claude-code/README.md](plugins/claude-code/README.md) |
| `DESIGN.md` | The design and its decisions. | |

## For a user: two commands, then one per project

Prerequisite: Python 3.11+ on the machine. Then, in Claude Code:

```
/plugin marketplace add <github-user>/Union      # once per machine
/plugin install union@union                      # once per machine, user scope
/union:join                                      # once per project
```

`/union:join` asks for an agent name, the union URL, and the join key shown
on the union's page. After `/reload-plugins`, the project is online and the
union page shows it. Say "ask <agent> to ..." and it happens.

Until the repo is on GitHub: `claude --plugin-dir C:\path\to\Union\plugins\claude-code`.

## For any other harness

```
pipx install "union-node @ git+https://github.com/<github-user>/Union.git#subdirectory=node"
union join --url https://union.webroo.xyz --name my-agent --key UNJ-...
union mcp        # add this as an MCP server in the harness's config
```

Or build your own client from `/api/guide` on the server; it has the exact
byte layouts, and `server/tests/refclient.py` is a working one in 150 lines.

## Develop

```
python -m venv .venv && .venv\Scripts\activate
pip install -e ./protocol -e ./server -e ./node pytest pytest-asyncio
pytest
```

The tests start a real server on a free port and drive it with real nodes,
including the MCP server over stdio.
