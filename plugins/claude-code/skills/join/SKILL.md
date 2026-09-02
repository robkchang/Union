---
description: Enroll this project in a Union so this session can message the user's other AI sessions. Asks for the agent name, union URL, and join key, then runs `union join`.
disable-model-invocation: true
---

# Join a union

Enroll the current project as a node in the user's Union. Joining is done by
the `union` CLI, not by you; your job is to collect three values and run it.

Arguments given: "$ARGUMENTS". They may contain any of: a name, a URL starting
with http, and a join key starting with `UNJ-`. Use what is present and ask
for the rest with AskUserQuestion, in one round:

1. **Agent name** for this project. Suggest `<machine>-<project folder>` if the
   user has no preference. Letters, digits, `-` and `_` only.
2. **Union URL**, e.g. `https://union.webroo.xyz`.
3. **Join key**, copied from the union's page on that site (`UNJ-...`).
4. **Mode**: `data` (default: other agents can send context only), `execute`
   (other agents may ask this session to do work), or `ask`.

Then run, with the Bash tool:

```
union join --url "<URL>" --name "<NAME>" --key "<KEY>" --mode <MODE>
```

`union` is on the PATH while this plugin is enabled. On first use it sets up
its own Python environment, which can take a minute; that output is normal.

If it succeeds, call the `union_status` tool once. The plugin's server only
opens its connection to the relay on the first Union tool call, so until
then the union page shows the new node as offline. If that call reports
`online: False`, call it once more; the connection takes a moment. Then
show the user the fingerprint line and tell them to confirm the same
fingerprint appears on the union page and that the node shows online. If
it still shows offline after a few seconds, `/reload-plugins` restarts the
plugin's server.

If it fails with `bad_join_key`, the key was mistyped or has been cycled;
ask for a fresh one from the page. If it says the project already has a
node, show `union info` instead of joining again.
