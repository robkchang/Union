from __future__ import annotations

import importlib.util
import json
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[3]
LAUNCH = ROOT / "plugins" / "codex" / "launch.py"


def _launch_module():
    spec = importlib.util.spec_from_file_location("union_codex_launch", LAUNCH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_launcher_forwards_the_workspace_as_project(monkeypatch, tmp_path):
    launch = _launch_module()
    monkeypatch.chdir(tmp_path)

    assert launch.with_workspace_project(["mcp", "--harness", "codex"]) == [
        "--project", str(tmp_path.resolve()), "mcp", "--harness", "codex",
    ]


def test_mcp_config_does_not_replace_the_workspace_cwd():
    config = json.loads((ROOT / "plugins" / "codex" / ".mcp.json").read_text("utf-8"))
    server = config["mcpServers"]["union"]

    assert "cwd" not in server
    assert server["args"][0] == "-c"
