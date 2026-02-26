import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

import openai
from openai import pydantic_function_tool

from ..cron.manager import CronJobManager
from ..mcp.manager import MCPManager
from ..memory.memory import MemoryUpdate, call_memory_update
from ..session.session import Session
from ..utils import message_to_dict, timestamp
from .prompts import build_agent_system_prompt


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
        self.cron_manager = CronJobManager(self.base_dir_path)

        self.announce_queue: asyncio.Queue[str] = asyncio.Queue()

    async def __aenter__(self):
        self.mcp_manager = await self.exit_stack.enter_async_context(
            MCPManager.load_from_config(self.mcp_config_json_path)
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.exit_stack.__aexit__(exc_type, exc_val, exc_tb)

    async def get_all_tools(self) -> list[dict]:
        native_tools = [pydantic_function_tool(MemoryUpdate)]
        mcp_tools = await self.mcp_manager.get_all_tools()
        return native_tools + mcp_tools

    async def call_tool(self, name, args) -> str:
        if name == MemoryUpdate.__name__:
            tool_response = await asyncio.to_thread(call_memory_update, self.workspace_path, **args)
        else:
            tool_response = await self.mcp_manager.call_tool(name, args)
        return tool_response

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

    async def run(self) -> None:
        await asyncio.gather(
            self.agent_loop(),
            self.cron_manager.scheduler_loop(self.run_cron_job),
        )

    async def agent_loop(self) -> None:
        sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
        session = Session.load(sessions_dir, "agent:main:cli:dm:local")
        tools = await self.get_all_tools()
        user_input_task = asyncio.create_task(asyncio.to_thread(input, "> "))
        while True:
            announce_queue_task = asyncio.create_task(self.announce_queue.get())

            done, pending = await asyncio.wait(
                [user_input_task, announce_queue_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            if announce_queue_task in done:
                msg = announce_queue_task.result()
                print(f"\n[📢 {msg}]\n")
                # user_input_taskはそのまま使い回す
                continue
            # ユーザー入力が来た
            announce_queue_task.cancel()
            try:
                await announce_queue_task
            except asyncio.CancelledError:
                pass
            user_message = user_input_task.result()
            if user_message.strip() == "/new":
                session.reset()
                print("[session reset]")
                user_input_task = asyncio.create_task(asyncio.to_thread(input, "> "))
                continue
            await self.assistant_turn(session, tools, user_message)
            user_input_task = asyncio.create_task(asyncio.to_thread(input, "> "))

    async def run_cron_job(self, job_id: str, message: str) -> None:
        sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
        session = Session.load(sessions_dir, f"cron:{job_id}-{timestamp()}")
        tools = await self.get_all_tools()
        assistant_message = await self.assistant_turn(session, tools, message)
        await self.announce_queue.put(
            f"[cron:{job_id}] {assistant_message.get('content') or '(no assistant message)'}"
        )

    async def assistant_turn(self, session: Session, tools: list[dict], user_message: str) -> dict:
        await session.append_message({"role": "user", "content": user_message})
        assistant_message = await self.chat_completions(session.messages, tools)
        await session.append_message(assistant_message)
        if tool_calls := assistant_message.get("tool_calls"):
            while tool_calls:
                for tool_call in tool_calls:
                    try:
                        tool_args = json.loads(tool_call["function"]["arguments"])
                        tool_response = await self.call_tool(
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
        return assistant_message
