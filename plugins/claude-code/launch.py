"""Launcher for the Union plugin. Makes the lift zero for the user: on first
run it installs the node's one dependency into a plain folder under the
plugin's data directory (no virtualenv: creating one takes over a minute on
some Windows machines), then runs the `union` CLI with PYTHONPATH pointing
at it.

    python launch.py --data <plugin-data-dir> [--project <dir>] <union args...>
    python launch.py --data <plugin-data-dir> bootstrap        # just ensure the install
    python launch.py --data <plugin-data-dir> session-start    # bootstrap, then tell the model
                                                               # how inbound messages arrive

Where the node code comes from:
* running from the repo checkout (`--plugin-dir plugins/claude-code`): the
  sibling `protocol/` and `node/` folders are used in place, so code changes
  apply immediately;
* installed from a marketplace: copied out of the marketplace's location
  (Claude Code records it in known_marketplaces.json), so the install keeps
  working if that location is refreshed or removed;
* otherwise: from UNION_NODE_SOURCE or the git URL below.

Anything printed during bootstrap goes to stderr, because stdout may be an
MCP transport or a hook's context channel. Several launchers can start at
once (SessionStart hook, MCP server, monitor); a lock file makes one of them
install while the others wait.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
GIT_SOURCE = "git+https://github.com/robchang/Union.git"
MARKER = f"union-node-installed-v2-py{sys.version_info[0]}.{sys.version_info[1]}"
LOCK_STALE_SECONDS = 15 * 60
WAIT_FOR_OTHER_INSTALLER = 10 * 60
SESSION_HINT = ("[union] Messages from your other agents arrive only while an inbox monitor "
                "runs. If this host does not start plugin monitors itself (VS Code, SDK), arm one "
                "now: a persistent Monitor running `union tail`. It exits at once if a monitor is "
                "already running, so arming it is always safe.")


def log(msg: str) -> None:
    print(f"[union plugin] {msg}", file=sys.stderr, flush=True)


def take(argv: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Pull `--flag value` out of argv. An unsubstituted ${VAR} counts as absent."""
    if flag in argv:
        i = argv.index(flag)
        val = argv[i + 1] if i + 1 < len(argv) else None
        rest = argv[:i] + argv[i + 2:]
        if val and not val.startswith("${"):
            return val, rest
        return None, rest
    return None, argv


def repo_sources() -> list[pathlib.Path] | None:
    """The repo checkout's protocol/ and node/ folders, when this plugin is
    loaded from it."""
    root = HERE.parent.parent
    proto, node = root / "protocol", root / "node"
    if (proto / "pyproject.toml").exists() and (node / "pyproject.toml").exists():
        return [proto, node]
    return None


def marketplace_sources() -> list[pathlib.Path] | None:
    plugins_dir = pathlib.Path.home() / ".claude" / "plugins"
    locations: list[pathlib.Path] = []
    known = plugins_dir / "known_marketplaces.json"
    if known.exists():
        try:
            for entry in json.loads(known.read_text("utf-8")).values():
                loc = entry.get("installLocation") or entry.get("source", {}).get("path")
                if loc:
                    locations.append(pathlib.Path(loc))
        except Exception:
            pass
    marketplaces = plugins_dir / "marketplaces"
    if marketplaces.exists():
        locations += [p for p in marketplaces.iterdir() if p.is_dir()]
    for mp in locations:
        proto, node = mp / "protocol", mp / "node"
        if (proto / "pyproject.toml").exists() and (node / "pyproject.toml").exists():
            return [proto, node]
    return None


def pip_install(site: pathlib.Path, specs: list[str]) -> None:
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "pip", "install", "-q", "--python", sys.executable, "--target", str(site), *specs],
                       check=True, stdout=sys.stderr)
        return
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--prefer-binary",
                    "--target", str(site), "--upgrade", *specs], check=True, stdout=sys.stderr)


def bootstrap(data: pathlib.Path) -> list[str]:
    """Ensure everything is installed. Returns the PYTHONPATH entries."""
    site = data / "site"
    marker = site / MARKER
    repo = repo_sources()
    path_entries = [str(p) for p in repo] + [str(site)] if repo else [str(site)]
    if marker.exists():
        return path_entries
    data.mkdir(parents=True, exist_ok=True)
    lock = data / "bootstrap.lock"
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > LOCK_STALE_SECONDS:
                lock.unlink(missing_ok=True)
                continue
            deadline = time.time() + WAIT_FOR_OTHER_INSTALLER
            while time.time() < deadline:
                if marker.exists():
                    return path_entries
                if not lock.exists():
                    break
                time.sleep(1)
            if marker.exists():
                return path_entries
    try:
        if repo:
            log("installing the node's dependency (one time, under a minute)")
            pip_install(site, ["cryptography>=42"])
        else:
            mp = marketplace_sources()
            if mp:
                # Both packages are pure Python: copy them rather than have
                # pip build wheels, which costs a minute each on slow machines.
                log("installing union-node from the marketplace checkout (one time, under a minute)")
                pip_install(site, ["cryptography>=42"])
                for src_root, pkg in ((mp[0], "union_protocol"), (mp[1], "union_node")):
                    dest = site / pkg
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(src_root / pkg, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                src = os.environ.get("UNION_NODE_SOURCE", GIT_SOURCE)
                log(f"installing union-node from {src} (one time)")
                if src.startswith("git+"):
                    pip_install(site, [f"union-protocol @ {src}#subdirectory=protocol",
                                       f"union-node @ {src}#subdirectory=node"])
                else:
                    pip_install(site, [src])
        site.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok")
    finally:
        lock.unlink(missing_ok=True)
    return path_entries


def default_data_dir() -> pathlib.Path:
    """The `union` wrapper on the Bash tool's PATH is not told the plugin's
    data directory. Reuse an install Claude Code already made for this
    plugin (under any of its ids) before creating a new one."""
    base = pathlib.Path.home() / ".claude" / "plugins" / "data"
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name.startswith("union") and (d / "site" / MARKER).exists():
                return d
    return base / "union-node"


def main() -> int:
    argv = sys.argv[1:]
    data_raw, argv = take(argv, "--data")
    project, argv = take(argv, "--project")
    data = pathlib.Path(data_raw) if data_raw else default_data_dir()
    quiet_fail = argv and argv[0] in ("bootstrap", "session-start", "status", "tail", "unread")
    try:
        path_entries = bootstrap(data)
    except Exception as exc:
        log(f"could not set up union-node: {exc}")
        return 0 if quiet_fail else 1
    if argv and argv[0] == "bootstrap":
        return 0
    if argv and argv[0] == "session-start":
        # SessionStart hook: stdout becomes context for the model. Hosts that
        # run plugin monitors (the terminal CLI) already tail the inbox; on
        # the others (VS Code, SDK) the model has to arm the monitor itself.
        print(SESSION_HINT, flush=True)
        return 0
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(path_entries + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    cmd = [sys.executable, "-m", "union_node.cli"]
    if project:
        cmd += ["--project", project]
    cmd += argv
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())
