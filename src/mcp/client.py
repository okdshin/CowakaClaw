from contextlib import AsyncExitStack
from types import TracebackType

from mcp.client.stdio import stdio_client

import mcp


class MCPClient:
    def __init__(
        self, server_name: str, command: str, args: list[str], env: dict[str, str] | None = None
    ):
        self.server_name = server_name
        self.command = command
        self.args = args
        self.env = env

        self.session: mcp.ClientSession | None = None
        self.exit_stack = AsyncExitStack()

    async def __aenter__(self) -> "MCPClient":
        server_params = mcp.StdioServerParameters(
            command=self.command, args=self.args, env=self.env
        )
        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        read, write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            mcp.ClientSession(read, write)
        )
        await self.session.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.exit_stack.aclose()

    async def list_tools(self) -> list[dict]:
        result = await self.session.list_tools()
        tools = []
        for tool in result.tools:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        # サーバー名プレフィックスで衝突を防ぐ
                        "name": f"{self.server_name}__{tool.name}",
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                }
            )
        return tools

    async def call_tool(self, tool_name: str, tool_args: dict) -> str:
        result = await self.session.call_tool(
            tool_name.removeprefix(f"{self.server_name}__"),
            tool_args,
        )

        # contentはリスト形式で返ってくる
        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
        output = "\n".join(texts) if texts else "(no output)"
        if result.isError:
            return f"Error: {output}"
        return output
