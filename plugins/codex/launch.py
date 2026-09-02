"""Launch Union's shared MCP server from the Codex plugin.

The checkout is used directly during development. Installed plugins fall back
to the published Union packages and keep their small dependency cache under
the user's Union data directory.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys


HERE = pathlib.Path(__file__).resolve().parent
GIT_SOURCE = "git+https://github.com/robkchang/Union.git"
SITE = pathlib.Path.home() / ".union" / "codex" / "site"
MARKER = SITE / f"installed-py{sys.version_info.major}.{sys.version_info.minor}"


def repo_sources() -> list[pathlib.Path] | None:
    root = HERE.parent.parent
    protocol, node = root / "protocol", root / "node"
    if (protocol / "pyproject.toml").exists() and (node / "pyproject.toml").exists():
        return [protocol, node]
    return None


def bundled_sources() -> list[pathlib.Path] | None:
    runtime = HERE / "runtime"
    if (runtime / "union_protocol").is_dir() and (runtime / "union_node").is_dir():
        return [runtime]
    return None


def install() -> list[str]:
    sources = repo_sources() or bundled_sources()
    if sources:
        if not MARKER.exists():
            SITE.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--prefer-binary",
                 "--target", str(SITE), "--upgrade", "cryptography>=42"],
                check=True,
                stdout=sys.stderr,
            )
            MARKER.write_text("ok", encoding="utf-8")
        return [*(str(source) for source in sources), str(SITE)]

    if not MARKER.exists():
        SITE.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--prefer-binary",
             "--target", str(SITE), "--upgrade",
             f"union-protocol @ {GIT_SOURCE}#subdirectory=protocol",
             f"union-node @ {GIT_SOURCE}#subdirectory=node"],
            check=True,
            stdout=sys.stderr,
        )
        MARKER.write_text("ok", encoding="utf-8")
    return [str(SITE)]


def with_workspace_project(argv: list[str]) -> list[str]:
    """Make Codex's current workspace explicit for the shared node CLI.

    The MCP bootstrap starts this launcher without a plugin-cache cwd, so the
    inherited cwd is the active workspace. Passing it explicitly prevents a
    later subprocess from accidentally resolving a `.union` in plugin state.
    """
    if "--project" in argv:
        return argv
    project = pathlib.Path.cwd().resolve()
    if "plugins" in project.parts and "cache" in project.parts:
        print("[union codex plugin] refusing to use the plugin cache as a project", file=sys.stderr, flush=True)
        return argv
    return ["--project", str(project), *argv]


def main() -> int:
    try:
        paths = install()
    except Exception as exc:
        print(f"[union codex plugin] could not prepare union-node: {exc}", file=sys.stderr, flush=True)
        return 1
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return subprocess.call([sys.executable, "-m", "union_node.cli", *with_workspace_project(sys.argv[1:])], env=env)


if __name__ == "__main__":
    sys.exit(main())
