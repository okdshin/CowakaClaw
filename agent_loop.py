import asyncio
import os
from collections.abc import AsyncGenerator
import json
from pathlib import Path
from contextlib import AsyncExitStack, asynccontextmanager

import openai
import mcp
from mcp.client.stdio import stdio_client


class MCPClient:
    def __init__(
        self, server_name, command: str, args: list[str], env: dict | None = None
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

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.exit_stack:
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
        return "\n".join(texts) if texts else "(no output)"


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


def build_agent_system_prompt(workspace_path: Path) -> str:
    # Tooling
    # Safety
    # Skills
    # Self-update
    # Workspace
    # Documentation
    # Sandbox
    # Project context
    bootstrap_prompts = []
    for md_filename in ["SOUL", "MEMORY"]:
        with open(workspace_path / f"{md_filename}.md") as mdf:
            md = mdf.read().strip()
            bootstrap_prompts.append(md)
    return "\n\n".join(bootstrap_prompts)


class CowakaClawAgent:
    def __init__(self, model, workspace_path: str, mcp_config_json_path: str):
        self.model = model
        self.workspace_path = Path(workspace_path)
        self.mcp_config_json_path = mcp_config_json_path

        self.openai_client = openai.AsyncOpenAI()
        self.exit_stack = AsyncExitStack()

    async def __aenter__(self):
        self.mcp_manager = await self.exit_stack.enter_async_context(
            MCPManager.load_from_config(self.mcp_config_json_path)
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.exit_stack.__aexit__(exc_type, exc_val, exc_tb)

    async def agent_loop(self):
        tools = await self.mcp_manager.get_all_tools()
        messages = []
        while True:
            user_message = await asyncio.to_thread(input, "> ")
            messages.append({"role": "user", "content": user_message})
            agent_system_prompt = build_agent_system_prompt(
                workspace_path=self.workspace_path
            )
            response = await self.openai_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": agent_system_prompt}]
                + messages,
                tools=tools,
            )
            if response.choices[0].message.role != "assistant":
                raise RuntimeError(f"{response.choices[0].message.role=} != assistant")
            messages.append(response.choices[0].message)
            if tool_calls := response.choices[0].message.tool_calls:
                while tool_calls:
                    for tool_call in tool_calls:
                        try:
                            tool_args = json.loads(tool_call.function.arguments)
                            tool_response = await self.mcp_manager.call_tool(
                                tool_call.function.name, tool_args
                            )
                        except json.JSONDecodeError:
                            tool_response = (
                                "tool_call.function.arguments JSON parse error"
                            )
                        except Exception as e:
                            tool_response = f"error: {type(e).__name__}: {e}"
                        messages.append(
                            {
                                "role": "tool",
                                "content": tool_response,
                                "tool_call_id": tool_call.id,
                            }
                        )
                        print(tool_response)
                    agent_system_prompt = build_agent_system_prompt(
                        workspace_path=self.workspace_path
                    )
                    response = await self.openai_client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": agent_system_prompt}]
                        + messages,
                        tools=tools,
                    )
                    if response.choices[0].message.role != "assistant":
                        raise RuntimeError(f"{response.choices[0].message.role=} != assistant")
                    messages.append(response.choices[0].message)
                    tool_calls = response.choices[0].message.tool_calls
            print(response.choices[0].message.content)


async def main():
    async with CowakaClawAgent(
        model="openai-gpt-oss-20b",
        workspace_path="./workspace",
        mcp_config_json_path="./mcp_config.json",
    ) as agent:
        await agent.agent_loop()


if __name__ == "__main__":
    asyncio.run(main())
