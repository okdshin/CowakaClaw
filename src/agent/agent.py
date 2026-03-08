import asyncio
import json
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Callable
from dataclasses import dataclass

import openai
from openai import pydantic_function_tool

from ..cron.manager import AddCronJobAt, AddCronJobCron, AddCronJobEvery, CronJobManager, DeleteCronJob
from ..mcp.manager import MCPManager
from ..memory.memory import MemoryUpdate, call_memory_update
from ..session.session import Session
from ..ui.base import IncomingMessage, UI
from ..utils import message_to_dict, timestamp
from .prompts import build_agent_system_prompt


@dataclass
class Tool:
    schema: str
    handler: Callable


class CowakaClawAgent:
    def __init__(
        self, model, base_dir_path: str, workspace_path: str, mcp_config_json_path: str, ui: UI
    ):
        self.model = model
        self.base_dir_path = Path(base_dir_path)
        self.workspace_path = Path(workspace_path)
        self.mcp_config_json_path = mcp_config_json_path
        self.ui = ui

        self.openai_client = openai.AsyncOpenAI()
        self.exit_stack = AsyncExitStack()
        self.cron_manager = CronJobManager(self.base_dir_path)

        self.announce_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # session_key → asyncio.Lock の弱参照辞書。
        # 値は弱参照で保持されるため、どのタスクも参照しなくなった時点でGCされ
        # エントリが自動消滅する。ロックが生きている間は、それを取得・待機している
        # タスクのローカル変数が強参照を持つことで存続する。
        self.session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

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
        async def handle_memory_update(args: dict, channel_id: str) -> str:
            return await asyncio.to_thread(call_memory_update, self.workspace_path, **args)
        register_tool("memory_update", MemoryUpdate, handle_memory_update)

        # Cron
        async def handle_cron_job_add_at(args: dict, channel_id: str) -> str:
            job_id = self.cron_manager.add_cron_job("at", args["at"], args["message"], args["name"], args.get("channel_id") or channel_id)
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_at", AddCronJobAt, handle_cron_job_add_at)

        async def handle_cron_job_add_cron(args: dict, channel_id: str) -> str:
            job_id = self.cron_manager.add_cron_job("cron", args["cron_expr"], args["message"], args["name"], args.get("channel_id") or channel_id)
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_cron", AddCronJobCron, handle_cron_job_add_cron)

        async def handle_cron_job_add_every(args: dict, channel_id: str) -> str:
            job_id = self.cron_manager.add_cron_job("every", str(args["interval_sec"]), args["message"], args["name"], args.get("channel_id") or channel_id)
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_every", AddCronJobEvery, handle_cron_job_add_every)

        async def handle_cron_job_delete(args: dict, channel_id: str) -> str:
            self.cron_manager.delete_cron_job(args["job_id"])
            return f"Cron job deleted: job_id={args['job_id']}"
        register_tool("cron_job_delete", DeleteCronJob, handle_cron_job_delete)

        return core_tools

    async def get_tools(self) -> list[dict]:
        return [tool.schema for tool in self.core_tools.values()] + await self.mcp_manager.get_all_tools()

    async def call_tool(self, tool_name: str, tool_args: dict, channel_id: str) -> str:
        if tool_name in self.core_tools:
            return await self.core_tools[tool_name].handler(tool_args, channel_id)
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
            self.dispatch_loop(),
            self.announce_loop(),
            self.cron_manager.scheduler_loop(self.run_cron_job),
            self.ui.start(),
        )

    async def dispatch_loop(self) -> None:
        """ui.receive() を回し続け、メッセージをセッションごとのタスクに振り分ける。"""
        tools = await self.get_tools()
        while True:
            message = await self.ui.receive()
            if self.ui.concurrent:
                asyncio.create_task(self.handle_message(message, tools))
            else:
                await self.handle_message(message, tools)

    async def announce_loop(self) -> None:
        """cronジョブの完了通知を受け取り、対象チャンネルに送信する。"""
        while True:
            channel_id, msg = await self.announce_queue.get()
            await self.ui.send(channel_id, f"\n[📢 {msg}]\n")

    async def handle_message(self, message: IncomingMessage, tools: list[dict]) -> None:
        """1メッセージを処理する。セッション単位で直列実行を保証する。

        同一セッションへの同時アクセスを防ぐためロックを使う。
        ロックはWeakValueDictionaryで管理し、そのセッションを処理中・待機中の
        タスクがなくなれば自動的にGCされる。

        get〜setの間にawaitがないため、asyncio単一スレッド上でアトミックに実行され
        複数タスクが同じキーに対して別々のロックを作ってしまうことはない。
        """
        try:
            lock = self.session_locks.get(message.session_key)
            if lock is None:
                lock = asyncio.Lock()
                self.session_locks[message.session_key] = lock

            async with lock:
                sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
                session = Session.load(sessions_dir, message.session_key)
                if message.content.strip() == "/new":
                    session.reset()
                    await self.ui.send(message.channel_id, "[session reset]")
                    return
                await self.assistant_turn(session, tools, message.content, message.channel_id)
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}", flush=True)

    async def run_cron_job(self, job_id: str, message: str, channel_id: str) -> None:
        sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
        session = Session.load(sessions_dir, f"cron:{job_id}-{timestamp()}")
        tools = await self.get_tools()
        assistant_message = await self.assistant_turn(session, tools, message, channel_id)
        await self.announce_queue.put(
            (channel_id, f"[cron:{job_id}] {assistant_message.get('content') or '(no assistant message)'}")
        )

    async def assistant_turn(self, session: Session, tools: list[dict], user_message: str, channel_id: str) -> dict:
        await session.append_message({"role": "user", "content": user_message})
        assistant_message = await self.chat_completions(session.messages, tools)
        await session.append_message(assistant_message)
        if tool_calls := assistant_message.get("tool_calls"):
            while tool_calls:
                for tool_call in tool_calls:
                    try:
                        tool_args = json.loads(tool_call["function"]["arguments"])
                        tool_response = await self.call_tool(
                            tool_call["function"]["name"], tool_args, channel_id
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
                    await self.ui.send(channel_id, tool_response)
                assistant_message = await self.chat_completions(
                    session.messages, tools
                )
                await session.append_message(assistant_message)
                tool_calls = assistant_message.get("tool_calls")
        await self.ui.send(channel_id, assistant_message.get("content") or "(no assistant message)")
        return assistant_message
