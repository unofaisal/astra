"""Example: wiring MCP servers into an Astra app.

Not imported anywhere automatically — copy what you need.

    from astra import create_astra
    from astra.tools.mcp_source import McpServerConfig

    app = create_astra(
        provider="openai", api_key="sk-...",
        mcp_servers=[
            # A local server over stdio (any language — this just needs
            # to speak MCP on stdin/stdout).
            McpServerConfig(
                name="filesystem",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/srv/workspace"],
                timeout=30,
            ),
            # A remote server over streamable HTTP, with an API key header
            # and a pinned schema hash (rug-pull protection).
            McpServerConfig(
                name="weather",
                url="https://weather.example.com/mcp",
                headers={"Authorization": "Bearer ..."},
                pin={"get_forecast": "<sha256 hash captured on first trusted connect>"},
                on_schema_change="block",
            ),
        ],
    )

    async def main():
        async with app:  # calls app.start() -> connects MCP servers; app.aclose() on exit
            result = await app.chat("what's the weather in Nairobi?")
            print(result.final_text)
"""
