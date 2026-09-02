---
description: "Set this project's Union mode to data (context only), execute (accept tasks from other agents), or ask (hold tasks for approval)."
disable-model-invocation: true
---

Set this node's mode with the Bash tool:

```
union mode $ARGUMENTS
```

Valid values: `data`, `execute`, `ask`. If "$ARGUMENTS" is empty or not one
of those, ask the user which they want and explain in one line each:
`data` means other agents can only send context; `execute` means they can
ask this session to do work, which then runs under this session's own
permissions; `ask` holds tasks until the user approves them.

Report the command's output. The change takes effect immediately; other
agents see the new mode in their agent list.
