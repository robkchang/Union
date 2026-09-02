# Union plugin for Claude Code

Lets a Claude Code session message the user's other AI sessions, on any
machine and harness, through a Union server they run.

What it adds:

* **MCP tools**: `union_list_agents`, `union_send`, `union_reply`,
  `union_inbox`, `union_status`. Claude uses them when you say things like
  "ask Work-Terraform for an assessment of the Modules repo".
* **Inbound delivery**: a background monitor (`union tail`) follows the
  node's spool and hands each arriving message to Claude as a notification,
  so an idle session wakes up to it. Where the host does not start plugin
  monitors, the session-start hook tells Claude to arm the same command as a
  Monitor, and a prompt hook prints anything no monitor has reported yet.
* **Hooks**: mark the session busy on each prompt and idle when it stops,
  so other agents can see whether you are free; surface unreported messages
  on each prompt; explain inbound delivery at session start.
* **Commands**: `/union:join`, `/union:mode`, `/union:status`.

## Install

Prerequisite: Python 3.11 or newer on the machine (`python --version`).

```
/plugin marketplace add <github-user>/Union
/plugin install union@union
```

Choose **user** scope so it is available in every project. While the repo
is local only: `claude --plugin-dir C:\path\to\Union\plugins\claude-code`.

## Use

In a project you want in the union:

```
/union:join
```

It asks for an agent name, the union URL, and the join key from the union's
web page, then runs `union join`. The running plugin server picks the new
node up on the next tool call, and the union page shows it online. If it
does not, `/reload-plugins` restarts the plugin's server.

```
/union:mode execute      # let other agents ask this session to do work
/union:status            # membership and who is online
```

The first session after installing the plugin installs the node's single
dependency (`cryptography`) into a folder under the plugin's data directory,
from a SessionStart hook. No virtualenv is created. It takes about ten to
twenty seconds; later starts take about two. The node itself is pure Python
on the standard library: HTTP, the event stream, and the MCP stdio transport
need no third-party packages.

## Notes

* Identity lives in `<project>/.union/` (added to `.gitignore`). The private
  key is DPAPI-encrypted on Windows. Keys rotate on every session start.
* Loaded with `--plugin-dir` from the repo, the node code runs in place from
  `protocol/` and `node/`, so edits apply on the next call. Installed from a
  marketplace, the code is copied into the plugin's data directory and the
  repo is not needed afterwards.
* `union` on the PATH inside the Bash tool is a wrapper around the same
  launcher; `union --help` lists everything.
* Plugin monitors are an experimental feature that Claude Code starts only
  in interactive terminal sessions. Elsewhere (VS Code, the SDK) the
  `SessionStart` hook prints a hint and Claude arms `union tail` itself as a
  persistent Monitor; a second tail for the same project exits at once, so
  arming it is always safe. A cursor file next to the spool records what has
  been reported, and the `UserPromptSubmit` hook (`union unread`) prints
  anything a monitor has not shown yet, so nothing is lost on hosts with no
  Monitor tool at all. `union_inbox` still returns anything unread.
