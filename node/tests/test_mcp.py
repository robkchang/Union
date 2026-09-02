"""Drive the real MCP server over stdio, the way a harness does."""
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from union_node.node import Node
from union_node.mcp_server import Tools, tool_defs


@pytest.mark.asyncio
async def test_mcp_server_over_stdio(union, tmp_path):
    d = tmp_path / "mcpproj"
    d.mkdir()
    Node.join(d, union["url"], "McpNode", union["join_key"], mode="execute")
    params = StdioServerParameters(command=sys.executable, args=["-m", "union_node.cli", "--project", str(d), "mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert "Union" in (init.instructions or "")
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == ["union_inbox", "union_list_agents", "union_reply", "union_send", "union_status"]
            send_tool = next(t for t in tools.tools if t.name == "union_send")
            assert set(send_tool.input_schema["properties"]) >= {"to", "text", "kind", "files", "wait_for_replies"}
            r = await session.call_tool("union_status", {})
            text = r.content[0].text
            assert "McpNode" in text and "mode: execute" in text
            r = await session.call_tool("union_list_agents", {})
            assert "No other agents" in r.content[0].text or "name | machine" in r.content[0].text
            r = await session.call_tool("union_send", {"to": "Nobody", "text": "hi"})
            assert "not_member" in r.content[0].text


@pytest.mark.asyncio
async def test_mcp_server_picks_up_a_late_join(union, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    params = StdioServerParameters(command=sys.executable, args=["-m", "union_node.cli", "--project", str(d), "mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool("union_inbox_hook", {})
            assert r.content[0].text == ""
            r = await session.call_tool("union_list_agents", {})
            assert "has not joined" in r.content[0].text
            # Join while the server is running: no restart needed.
            Node.join(d, union["url"], "LateJoiner", union["join_key"], mode="data")
            r = await session.call_tool("union_status", {})
            assert "LateJoiner" in r.content[0].text


def test_mcp_join_creates_a_codex_node(union, tmp_path):
    project = tmp_path / "codex-project"
    project.mkdir()

    result = Tools(None, project, harness="codex").join(union["url"], "CodexNode", union["join_key"], "execute")

    node = Node.load(project)
    try:
        assert "Joined Union" in result
        assert node.cfg.name == "CodexNode"
        assert node.cfg.harness == "codex"
        assert node.cfg.mode == "execute"
    finally:
        node.client.leave()
        node.stop()


def test_mcp_join_tool_is_codex_only():
    claude_tools = {tool["name"] for tool in tool_defs("claude-code")}
    codex_tools = {tool["name"] for tool in tool_defs("codex")}

    assert "union_join" not in claude_tools and "union_inbox_hook" not in claude_tools
    assert "union_join" in codex_tools and "union_inbox_hook" in codex_tools
