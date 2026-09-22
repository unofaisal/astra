"""A tiny MCP server used only by tests/test_mcp_source.py."""
import asyncio
import sys

from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
def fail() -> str:
    """Always fails."""
    raise RuntimeError("nope")


@mcp.tool()
async def slow(seconds: float = 1.0) -> str:
    """Sleep for a while."""
    await asyncio.sleep(seconds)
    return "done"


if __name__ == "__main__":
    mcp.run("stdio")
