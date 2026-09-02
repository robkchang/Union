"""`union` command line.

    union join --url URL --name NAME --key KEY [--mode data|execute|ask] [--harness H]
    union info | agents | mode MODE | trust NAME | leave
    union send --to NAME[,NAME|*|mode:execute] --text TEXT [--file PATH]... [--kind data|task] [--wait]
    union listen                      print messages as they arrive (any harness, or testing)
    union status busy|idle            used by harness hooks; reads hook JSON on stdin for cwd
    union mcp                         run the MCP server over stdio
    union tail                        follow the spool; used as a harness monitor
    union unread                      hook: print messages no monitor has reported yet
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import socket
import sys
import time

from . import config as cfgmod
from . import spool as spoolmod
from .client import UnionError
from .node import Node


def _project(args) -> pathlib.Path | None:
    start = pathlib.Path(args.project) if getattr(args, "project", None) else None
    return cfgmod.find_project_dir(start)


def _load(args) -> Node:
    project = _project(args)
    if not project:
        sys.exit("No Union node here. Run `union join` in the project first.")
    return Node.load(project)


def cmd_join(args) -> int:
    project = pathlib.Path(args.project or pathlib.Path.cwd()).resolve()
    url = args.url or input("Union URL: ").strip()
    default_name = f"{socket.gethostname()}-{project.name}"
    name = args.name or (input(f"Agent name [{default_name}]: ").strip() or default_name)
    key = args.key or input("Join key: ").strip()
    try:
        node = Node.join(project, url, name, key, mode=args.mode, harness=args.harness)
    except UnionError as exc:
        sys.exit(f"Join failed: {exc.code}: {exc.detail}")
    except Exception as exc:
        sys.exit(f"Join failed: {exc}")
    info = node.info()
    print(f"Joined union \"{info['union']}\" as {info['name']} (mode: {info['mode']}).")
    print(f"Your fingerprint: {info['fingerprint']}   server: {info['hub_fingerprint']}")
    print(f"Check the union page shows {info['name']} with that fingerprint.")
    print(f"Identity saved in {cfgmod.union_dir(project)} (added to .gitignore).")
    return 0


def cmd_info(args) -> int:
    node = _load(args)
    for k, v in node.info().items():
        print(f"{k:16} {v}")
    return 0


def cmd_agents(args) -> int:
    node = _load(args)
    agents = node.agents()
    if not agents:
        print("No other agents online.")
        return 0
    print(f"{'name':24} {'machine':16} {'harness':12} {'mode':8} {'status':8} last seen")
    for a in agents:
        print(f"{a['name']:24} {a['machine']:16} {a['harness']:12} {a['mode']:8} {a['status']:8} {a['last_seen_at']}")
    return 0


def cmd_mode(args) -> int:
    node = _load(args)
    node.set_mode(args.mode)
    print(f"{node.cfg.name} is now in {args.mode} mode.")
    return 0


def cmd_trust(args) -> int:
    node = _load(args)
    node._accept_roster(node.client.roster())
    node.trust(args.name)
    print(f"Pinned {args.name}'s current keys.")
    return 0


def cmd_leave(args) -> int:
    node = _load(args)
    try:
        node.client.leave()
    except UnionError as exc:
        print(f"Server said {exc.code}; removing local identity anyway.")
    d = cfgmod.union_dir(node.project_dir)
    for f in (cfgmod.CONFIG_FILE, cfgmod.KEY_FILE, cfgmod.SPOOL_FILE):
        try:
            (d / f).unlink()
        except FileNotFoundError:
            pass
    print(f"Left union \"{node.cfg.union_name}\". Local identity removed.")
    return 0


def cmd_send(args) -> int:
    node = _load(args)
    node.start(rotate=False)
    try:
        for _ in range(50):
            if node.online:
                break
            time.sleep(0.1)
        r = node.send(args.to, args.text, kind=args.kind, files=args.file or [], wait_for_replies=args.wait,
                      timeout=args.timeout)
        for x in r["results"]:
            print(f"{x['to']}: {x['status']}" + (f" ({x['detail']})" if x.get("detail") and x["status"] != "sent" else ""))
        for m in r.get("replies", []):
            print(f"\n--- reply from {m.from_name} ---\n{m.text}")
    finally:
        node.stop()
    return 0


def cmd_listen(args) -> int:
    node = _load(args)
    node.start(rotate=False)
    print(f"Listening as {node.cfg.name}. Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            for m in node.drain_inbox():
                print(m.framed(), flush=True)
            if node.evicted_reason:
                print(f"[union] evicted: {node.evicted_reason}", flush=True)
                return 1
            time.sleep(0.3)
    except KeyboardInterrupt:
        return 0
    finally:
        node.stop()


def cmd_status(args) -> int:
    """Harness hook. Must print nothing and exit 0 whatever happens."""
    project = _hook_project(args)
    if not project:
        return 0
    try:
        node = Node.load(project)
        node.client.presence(status=args.state)
    except Exception:
        pass
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import main as mcp_main
    return mcp_main(pathlib.Path(args.project) if args.project else None)


def _hook_project(args) -> pathlib.Path | None:
    """Hooks get JSON on stdin with the session's cwd; fall back to --project."""
    cwd = None
    if not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            cwd = payload.get("cwd")
        except Exception:
            cwd = None
    start = pathlib.Path(cwd) if cwd else (pathlib.Path(args.project) if args.project else None)
    return cfgmod.find_project_dir(start)


def cmd_tail(args) -> int:
    """Follow the spool and print one line per message. Picks up where the
    last reader left off (the cursor), or at the end on a first run, so only
    new messages are reported. If this project is not in a union yet, waits
    quietly for a `union join`. One tail per project: a second one exits at
    once, so a harness or the model can arm it without checking first."""
    lock = None
    try:
        project = _project(args)
        while not project:
            time.sleep(2)
            project = _project(args)
        d = cfgmod.union_dir(project)
        lock = spoolmod.acquire_tail_lock(d)
        if lock is None:
            print("[union] an inbox monitor is already running for this project", file=sys.stderr, flush=True)
            return 0
        path = d / cfgmod.SPOOL_FILE
        pos = spoolmod.start_position(d, path)
        spoolmod.write_cursor(d, pos)
        while True:
            lines, new_pos = spoolmod.read_new(path, pos)
            if new_pos != pos:
                pos = new_pos
                spoolmod.write_cursor(d, pos)
            for line in lines:
                print(line, flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        spoolmod.release_tail_lock(lock)


def cmd_unread(args) -> int:
    """Harness hook (UserPromptSubmit): print spool lines no reader has
    reported yet, so a host without monitors still surfaces messages on the
    next prompt. Prints nothing when a tail is keeping up. Exits 0 always."""
    try:
        project = _hook_project(args)
        if not project:
            return 0
        d = cfgmod.union_dir(project)
        path = d / cfgmod.SPOOL_FILE
        pos = spoolmod.start_position(d, path)
        lines, new_pos = spoolmod.read_new(path, pos)
        # Always persist: on a first run this pins the cursor to the end so
        # only messages from now on are reported next time.
        spoolmod.write_cursor(d, new_pos)
        for line in lines:
            print(line, flush=True)
    except Exception:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="union", description="Union node: message your other AI sessions.")
    p.add_argument("--project", help="Project directory (default: walk up from the current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("join", help="Join a union in this project")
    j.add_argument("--url"); j.add_argument("--name"); j.add_argument("--key")
    j.add_argument("--mode", choices=["data", "execute", "ask"], default="data")
    j.add_argument("--harness", default="claude-code")
    j.set_defaults(fn=cmd_join)

    sub.add_parser("info", help="Show this node").set_defaults(fn=cmd_info)
    sub.add_parser("agents", help="Who is online").set_defaults(fn=cmd_agents)
    m = sub.add_parser("mode", help="Set this node's mode")
    m.add_argument("mode", choices=["data", "execute", "ask"]); m.set_defaults(fn=cmd_mode)
    t = sub.add_parser("trust", help="Accept a peer's changed keys")
    t.add_argument("name"); t.set_defaults(fn=cmd_trust)
    sub.add_parser("leave", help="Leave the union and delete the local identity").set_defaults(fn=cmd_leave)

    s = sub.add_parser("send", help="Send a message")
    s.add_argument("--to", required=True); s.add_argument("--text", required=True)
    s.add_argument("--kind", choices=["data", "task"], default="data")
    s.add_argument("--file", action="append"); s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=float, default=600); s.set_defaults(fn=cmd_send)

    sub.add_parser("listen", help="Print messages as they arrive").set_defaults(fn=cmd_listen)
    st = sub.add_parser("status", help="Hook: set busy or idle")
    st.add_argument("state", choices=["busy", "idle"]); st.set_defaults(fn=cmd_status)
    sub.add_parser("mcp", help="Run the MCP server (stdio)").set_defaults(fn=cmd_mcp)
    sub.add_parser("tail", help="Follow the inbox spool (harness monitor)").set_defaults(fn=cmd_tail)
    sub.add_parser("unread", help="Hook: print messages no monitor has reported yet").set_defaults(fn=cmd_unread)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
