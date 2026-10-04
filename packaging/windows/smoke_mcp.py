"""Initialize bundled Brave over stdio without making a paid search request."""
import argparse
import asyncio
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(node: Path, entry: Path):
    parameters = StdioServerParameters(
        command=str(node.absolute()), args=[str(entry.resolve()), "--transport", "stdio"],
        env={**os.environ, "BRAVE_API_KEY": "momoi-packaging-initialization-check"},
    )
    async with asyncio.timeout(30):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"brave_web_search", "brave_local_search"} <= names, names
    print("Bundled Brave MCP stdio initialization and tool discovery passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.node, args.entry))
