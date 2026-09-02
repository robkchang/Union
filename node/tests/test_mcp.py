"""Drive the real MCP server over stdio, the way a harness does."""
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from union_node.node import Node


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
            r = await session.call_tool("union_list_agents", {})
            assert "has not joined" in r.content[0].text
            # Join while the server is running: no restart needed.
            Node.join(d, union["url"], "LateJoiner", union["join_key"], mode="data")
            r = await session.call_tool("union_status", {})
            assert "LateJoiner" in r.content[0].text
