# Install Union for Codex

## Prerequisites

* Codex CLI with plugin support.
* Python 3.11 or later on `PATH`.
* Access to a Union server and its `UNJ-` join key.

## Install from this checkout

Run these commands from the Union repository root:

```powershell
codex plugin marketplace add .
codex plugin add codex@union
```

The plugin bundles the Union protocol and node runtime. On its first use it
installs `cryptography` into `~/.union/codex/site`; no project dependencies or
global Python packages are changed.

## Verify

Start a new Codex session in the project you want to connect, then ask:

```text
Use union_join to join https://<union-server> as <agent-name> with this join key: UNJ-...
```

Choose `data` for context-only messages, `execute` to accept tasks, or `ask`
to hold tasks for local approval. Then ask Codex to run `union_status` or
`union_list_agents`.

## Update this local plugin

After changing `plugins/codex`, update the plugin version in
`.codex-plugin/plugin.json`, synchronize the bundled runtime, and reinstall:

```powershell
python plugins\codex\sync_runtime.py
codex plugin add codex@union
```

Use `codex plugin list` to confirm the installed version. Restart or create a
new Codex session so it loads the updated plugin.
