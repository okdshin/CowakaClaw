import asyncio
from datetime import datetime
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


class Session:
    def __init__(
        self,
        session_id: str,
        session_key: str,
        messages: list[dict],
        session_jsonl_path: Path,
        sessions_json_path: Path,
    ):
        self.session_id = session_id
        self.session_key = session_key
        self.messages = messages
        self.session_jsonl_path = session_jsonl_path
        self.sessions_json_path = sessions_json_path

    @classmethod
    def load(cls, sessions_dir: Path, session_key: str) -> "Session":
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_json_path = sessions_dir / "sessions.json"

        # sessions.json を読む（なければ空dict）
        if sessions_json_path.exists():
            with open(sessions_json_path) as f:
                sessions = json.load(f)
        else:
            sessions = {}

        # session_key に対応する session_id を解決（なければ新規発行）
        entry = sessions.get(session_key)
        if entry is not None:
            session_id = entry["sessionId"]
        else:
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            sessions[session_key] = {
                "sessionId": session_id,
                "updatedAt": datetime.now().isoformat(),
            }
            with open(sessions_json_path, "w") as f:
                json.dump(sessions, f, indent=2)

        # JSONL を読んで messages を復元（なければ空リスト）
        session_jsonl_path = sessions_dir / f"{session_id}.jsonl"
        messages = []
        if session_jsonl_path.exists():
            with open(session_jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        messages.append(json.loads(line))

        return cls(
            session_id=session_id,
            session_key=session_key,
            messages=messages,
            session_jsonl_path=session_jsonl_path,
            sessions_json_path=sessions_json_path,
        )

    async def append_message(self, message: dict) -> None:
        self.messages.append(message)
        await asyncio.to_thread(self._write_message, message)

    def _write_message(self, message: dict) -> None:
        with open(self.session_jsonl_path, "a") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._update_sessions_json()

    def reset(self) -> None:
        self.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_jsonl_path = (
            self.session_jsonl_path.parent / f"{self.session_id}.jsonl"
        )
        self.messages = []
        self._update_sessions_json()
        # keep old session jsonl files

    def _update_sessions_json(self) -> None:
        if self.sessions_json_path.exists():
            with open(self.sessions_json_path) as f:
                sessions = json.load(f)
        else:
            sessions = {}
        sessions[self.session_key] = {
            "sessionId": self.session_id,
            "updatedAt": datetime.now().isoformat(),
        }
        with open(self.sessions_json_path, "w") as f:
            json.dump(sessions, f, indent=2)


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


def message_to_dict(message) -> dict:
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return message


class CowakaClawAgent:
    def __init__(
        self, model, base_dir_path: str, workspace_path: str, mcp_config_json_path: str
    ):
        self.model = model
        self.base_dir_path = Path(base_dir_path)
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

    async def chat_completions(self, messages, tools) -> dict:
        agent_system_prompt = build_agent_system_prompt(
            workspace_path=self.workspace_path
        )
        response = await self.openai_client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": agent_system_prompt}] + messages,
            tools=tools or None,
        )
        if response.choices[0].message.role != "assistant":
            raise RuntimeError(f"{response.choices[0].message.role=} != assistant")
        return message_to_dict(response.choices[0].message)

    async def agent_loop(self):
        sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
        session = Session.load(sessions_dir, "agent:main:cli:dm:local")
        tools = await self.mcp_manager.get_all_tools()
        while True:
            user_message = await asyncio.to_thread(input, "> ")
            if user_message.strip() == "/new":
                session.reset()
                print("[session reset]")
                continue
            await session.append_message({"role": "user", "content": user_message})
            assistant_message = await self.chat_completions(session.messages, tools)
            await session.append_message(assistant_message)
            if tool_calls := assistant_message.get("tool_calls"):
                while tool_calls:
                    for tool_call in tool_calls:
                        try:
                            tool_args = json.loads(tool_call["function"]["arguments"])
                            tool_response = await self.mcp_manager.call_tool(
                                tool_call["function"]["name"], tool_args
                            )
                        except json.JSONDecodeError:
                            tool_response = (
                                "tool_call.function.arguments JSON parse error"
                            )
                        except Exception as e:
                            tool_response = f"error: {type(e).__name__}: {e}"
                        await session.append_message(
                            {
                                "role": "tool",
                                "content": tool_response,
                                "tool_call_id": tool_call["id"],
                            },
                        )
                        print(tool_response)
                    assistant_message = await self.chat_completions(
                        session.messages, tools
                    )
                    await session.append_message(assistant_message)
                    tool_calls = assistant_message.get("tool_calls")
            print(assistant_message.get("content") or "(no assistant message)")


async def main():
    async with CowakaClawAgent(
        model="openai-gpt-oss-20b",
        base_dir_path="./base_dir",
        workspace_path="./workspace",
        mcp_config_json_path="./mcp_config.json",
    ) as agent:
        await agent.agent_loop()


if __name__ == "__main__":
    asyncio.run(main())
