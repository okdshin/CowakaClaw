import json
import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

from .client import MCPClient


class MCPManager:
    def __init__(self, configs: dict[str, dict]):
        self.configs = configs
        self.clients: dict[str, MCPClient] = {}  # server_name → MCPClient
        self.exit_stack = AsyncExitStack()

    async def __aenter__(self):
        for server_name, server_config in self.configs.items():
            try:
                client = await self.exit_stack.enter_async_context(
                    MCPClient(
                        server_name=server_name,
                        command=server_config["command"],
                        args=server_config.get("args", []),
                        env={**os.environ, **server_config.get("env", {})},
                    )
                )
                self.clients[server_name] = client
                print(f"[MCP] connected: {server_name}")
            except Exception as e:
                print(f'[MCP] "{server_name}" connect error: {e}')
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.exit_stack.__aexit__(exc_type, exc_val, exc_tb)

    @classmethod
    @asynccontextmanager
    async def load_from_config(
        cls, mcp_config_json_path: str
    ) -> AsyncGenerator["MCPManager", None]:
        with open(mcp_config_json_path) as f:
            config = json.load(f)
        if "mcpServers" not in config:
            raise ValueError(f"'mcpServers' key not found in {mcp_config_json_path}")
        async with MCPManager(configs=config["mcpServers"]) as mcp_manager:
            yield mcp_manager

    async def get_all_tools(self) -> list[dict]:
        all_tools = []
        for server_name, client in self.clients.items():
            tools = await client.list_tools()
            all_tools.extend(tools)
        return all_tools

    async def call_tool(self, tool_name: str, tool_args: dict) -> str:
        server_name, _ = tool_name.split("__", 1)
        client = self.clients.get(server_name)
        if not client:
            return f"Error: unknown server '{tool_name}'"
        return await client.call_tool(tool_name, tool_args)
