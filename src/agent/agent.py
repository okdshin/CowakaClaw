import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Callable
from dataclass import dataclass

import openai
from openai import pydantic_function_tool

from ..cron.manager import AddCronJobAt, AddCronJobCron, AddCronJobEvery, CronJobManager, DeleteCronJob
from ..mcp.manager import MCPManager
from ..memory.memory import MemoryUpdate, call_memory_update
from ..session.session import Session
from ..tools.registry import ToolRegistry
from ..utils import message_to_dict, timestamp
from .prompts import build_agent_system_prompt


@dataclass
class Tool:
    schema: str
    handler: Callable


async def read_stdin_line(prompt: str) -> str:
    """スレッドをブロックせずに stdin から1行読む。CancelledError に対応。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def on_readable() -> None:
        loop.remove_reader(sys.stdin.fileno())
        line = sys.stdin.readline()
        if not fut.done():
            fut.set_result(line.rstrip("\n"))

    sys.stdout.write(prompt)
    sys.stdout.flush()
    loop.add_reader(sys.stdin.fileno(), on_readable)
    try:
        return await fut
    except asyncio.CancelledError:
        loop.remove_reader(sys.stdin.fileno())
        raise


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
        self.core_tools = await self.build_core_tools()
        self.mcp_manager = await self.exit_stack.enter_async_context(
            MCPManager.load_from_config(self.mcp_config_json_path)
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.exit_stack.__aexit__(exc_type, exc_val, exc_tb)

    async def build_core_tools(self) -> dict[str, Tool]:
        core_tools: dict[str, Tool] = {}

        def register_tool(name, pydantic_model, handler):
            core_tools[name] = Tool(
                schema=pydantic_function_tool(pydantic_model, name=name),
                handler=handler,
            )

        # Memory
        async def handle_memory_update(args: dict) -> str:
            return await asyncio.to_thread(call_memory_update, self.workspace_path, **args)
        register_tool("memory_update", MemoryUpdate, handle_memory_update)

        # Cron
        async def handle_cron_job_add_at(args: dict) -> str:
            job_id = self.cron_manager.add_cron_job("at", args["at"], args["message"], args["name"])
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_at", AddCronJobAt, handle_cron_job_add_at)

        async def handle_cron_job_add_cron(args: dict) -> str:
            job_id = self.cron_manager.add_cron_job("cron", args["cron_expr"], args["message"], args["name"])
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_cron", AddCronJobCron, handle_cron_job_add_cron)

        async def handle_cron_job_add_every(args: dict) -> str:
            job_id = self.cron_manager.add_cron_job("every", str(args["interval_sec"]), args["message"], args["name"])
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_every", AddCronJobEvery, handle_cron_job_add_every)

        async def handle_cron_job_delete(args: dict) -> str:
            self.cron_manager.delete_cron_job(args["job_id"])
            return f"Cron job deleted: job_id={args['job_id']}"
        register_tool("cron_job_delete", DeleteCronJob, handle_cron_job_delete)

        return core_tools

    async def get_tools(self) -> list[dict]:
        return [tool.schema for tool in self.core_tools] + self.mcp_manager.get_all_tools()

    async def call_tool(self, tool_name: str, tool_args: dict) -> str:
        if tool_name in self.core_tools:
            return await self.core_tools[tool_name].handler(tool_args)
        else:
            return await self.mcp_manager.call_tool(tool_name, tool_args)

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
        tools = self.get_tools()
        user_input_task = asyncio.create_task(read_stdin_line("> "))
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
                user_input_task = asyncio.create_task(read_stdin_line("> "))
                continue
            await self.assistant_turn(session, tools, user_message)
            user_input_task = asyncio.create_task(read_stdin_line("> "))

    async def run_cron_job(self, job_id: str, message: str) -> None:
        sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
        session = Session.load(sessions_dir, f"cron:{job_id}-{timestamp()}")
        tools = self.get_tools()
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
                        tool_response = self.call_tool(
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
