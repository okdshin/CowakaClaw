import asyncio
import json
import logging
import uuid
import weakref
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Awaitable, Callable

import openai
from openai import pydantic_function_tool

from ..cron.manager import AddCronJobAt, AddCronJobCron, AddCronJobEvery, CronJobManager, DeleteCronJob
from ..mcp.manager import MCPManager
from ..memory.memory import MemoryUpdate, call_memory_update
from ..session.session import Session
from ..ui.base import UI, IncomingMessage
from .prompts import build_agent_system_prompt

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    schema: dict
    handler: Callable[[dict, str], Awaitable[str]]


class CowakaClawAgent:
    def __init__(
        self,
        model: str,
        base_dir_path: str,
        workspace_path: str,
        mcp_config_json_path: str,
        ui: UI,
        max_tool_iterations: int | None = None,
        llm_timeout: float | None = None,
        openai_client: openai.AsyncOpenAI | None = None,
    ):
        self.model = model
        self.base_dir_path = Path(base_dir_path)
        self.workspace_path = Path(workspace_path)
        self.mcp_config_json_path = mcp_config_json_path
        self.ui = ui
        self.max_tool_iterations = max_tool_iterations
        self.llm_timeout = llm_timeout

        self.openai_client = openai_client if openai_client is not None else openai.AsyncOpenAI()
        self.exit_stack = AsyncExitStack()
        self.cron_manager = CronJobManager(self.base_dir_path)

        self.announce_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # session_key → asyncio.Lock の弱参照辞書。
        # 値は弱参照で保持されるため、どのタスクも参照しなくなった時点でGCされ
        # エントリが自動消滅する。ロックが生きている間は、それを取得・待機している
        # タスクのローカル変数が強参照を持つことで存続する。
        self.session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def __aenter__(self) -> "CowakaClawAgent":
        self.core_tools = await self.build_core_tools()
        self.mcp_manager = await self.exit_stack.enter_async_context(
            MCPManager.load_from_config(self.mcp_config_json_path)
        )
        self.tools = await self.get_tools()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self.exit_stack.__aexit__(exc_type, exc_val, exc_tb)

    async def build_core_tools(self) -> dict[str, Tool]:
        core_tools: dict[str, Tool] = {}

        def register_tool(
            name: str,
            pydantic_model: type,
            handler: Callable[[dict, str], Awaitable[str]],
        ) -> None:
            core_tools[name] = Tool(
                schema=pydantic_function_tool(pydantic_model, name=name),
                handler=handler,
            )

        # Memory
        async def handle_memory_update(args: dict, channel_id: str) -> str:
            return await asyncio.to_thread(call_memory_update, self.workspace_path, **args)
        register_tool("memory_update", MemoryUpdate, handle_memory_update)

        # Cron（supports_cron=False のUIでは登録しない）
        if not self.ui.supports_cron:
            return core_tools

        async def handle_cron_job_add_at(args: dict, channel_id: str) -> str:
            job_id = await self.cron_manager.add_cron_job(
                "at", args["at"], args["message"], args["name"], args.get("channel_id") or channel_id
            )
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_at", AddCronJobAt, handle_cron_job_add_at)

        async def handle_cron_job_add_cron(args: dict, channel_id: str) -> str:
            job_id = await self.cron_manager.add_cron_job(
                "cron", args["cron_expr"], args["message"], args["name"], args.get("channel_id") or channel_id
            )
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_cron", AddCronJobCron, handle_cron_job_add_cron)

        async def handle_cron_job_add_every(args: dict, channel_id: str) -> str:
            job_id = await self.cron_manager.add_cron_job(
                "every", str(args["interval_sec"]), args["message"], args["name"], args.get("channel_id") or channel_id
            )
            return f"Cron job created: job_id={job_id}"
        register_tool("cron_job_add_every", AddCronJobEvery, handle_cron_job_add_every)

        async def handle_cron_job_delete(args: dict, channel_id: str) -> str:
            try:
                await self.cron_manager.delete_cron_job(args["job_id"])
            except KeyError as e:
                return f"Error: {e}"
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

    async def _chat_completions_non_stream(self, messages: list[dict], tools: list[dict]) -> dict:
        agent_system_prompt = build_agent_system_prompt(
            workspace_path=self.workspace_path
        )
        response = await self.openai_client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": agent_system_prompt}] + messages,
            tools=tools or None,
            timeout=self.llm_timeout,
        )
        if response.choices[0].message.role != "assistant":
            raise RuntimeError(f"{response.choices[0].message.role=} != assistant")
        message = response.choices[0].message
        assert hasattr(message, "model_dump")
        return message.model_dump(exclude_none=True)

    async def _chat_completions_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        on_chunk: Callable[[str], Awaitable[None]],
    ) -> dict:
        """ストリーミングでLLMを呼び出す。テキストレスポンスの各チャンクを on_chunk に渡す。
        ツールコールレスポンスの場合は on_chunk を呼ばずにそのままアセンブルして返す。
        """
        agent_system_prompt = build_agent_system_prompt(workspace_path=self.workspace_path)
        stream = await self.openai_client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": agent_system_prompt}] + messages,
            tools=tools or None,
            stream=True,
            timeout=self.llm_timeout,
        )

        content = ""
        tool_calls: dict[int, dict] = {}  # index → 累積中のtool_call
        is_tool_response = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                content += delta.content
                if not is_tool_response:
                    await on_chunk(delta.content)

            if delta.tool_calls:
                is_tool_response = True
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.id:
                        tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls[idx]["function"]["arguments"] += tc.function.arguments

        msg: dict = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls.keys())]
        return msg

    async def _chat_completions(
        self, messages: list[dict], tools: list[dict], channel_id: str, stream: bool
    ) -> dict:
        """stream フラグに応じて chat_completions / chat_completions_stream を呼び分ける。"""
        if stream:
            return await self._chat_completions_stream(
                messages,
                tools,
                on_chunk=lambda c: self.ui.send_stream_chunk(channel_id, c),
            )
        return await self._chat_completions_non_stream(messages, tools)

    async def run(self) -> None:
        await asyncio.gather(
            self.dispatch_loop(),
            self.announce_loop(),
            self.cron_manager.scheduler_loop(self.run_cron_job),
            self.ui.start(),
        )

    async def dispatch_loop(self) -> None:
        """ui.receive() を回し続け、メッセージをセッションごとのタスクに振り分ける。"""
        while True:
            message = await self.ui.receive()
            if self.ui.concurrent:
                asyncio.create_task(self.handle_message(message, self.tools))
            else:
                await self.handle_message(message, self.tools)

    async def announce_loop(self) -> None:
        """cronジョブの完了通知を受け取り、対象チャンネルに送信する。"""
        while True:
            channel_id, msg = await self.announce_queue.get()
            try:
                await self.ui.send(channel_id, f"\n[📢 {msg}]\n")
            except Exception as e:
                logger.error("announce send error: %s: %s", type(e).__name__, e)

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
                if message.messages_override is not None:
                    # API UIからの場合: クライアントが全履歴を送ってくるのでそのまま使う。
                    # セッションファイルへの永続化は行わない。
                    await self.assistant_turn_stateless(
                        message.messages_override, tools, message.channel_id, stream=message.stream
                    )
                    return

                sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
                session = await Session.load(sessions_dir, message.session_key)
                if message.content.strip() == "/new":
                    await asyncio.to_thread(session.reset)
                    await self.ui.send(message.channel_id, "[session reset]")
                    return
                await self.assistant_turn(session, tools, message.content, message.channel_id, stream=message.stream)
        except Exception as e:
            logger.error("%s: %s", type(e).__name__, e)
            await self.ui.send(message.channel_id, f"[エラーが発生しました: {type(e).__name__}: {e}]")

    async def run_cron_job(self, job_id: str, message: str, channel_id: str) -> None:
        sessions_dir = self.base_dir_path / "agents" / "main" / "sessions"
        fired_at = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        session = await Session.load(sessions_dir, f"cron:{job_id}-{fired_at}-{uuid.uuid4().hex[:8]}")
        try:
            assistant_message = await self.assistant_turn(session, self.tools, message, channel_id)
            result = assistant_message.get('content') or '(no assistant message)'
        except Exception as e:
            logger.error("cron:%s error: %s: %s", job_id, type(e).__name__, e)
            result = f"error: {type(e).__name__}: {e}"
        await self.announce_queue.put((channel_id, f"[cron:{job_id}] {result}"))

    async def assistant_turn(
        self, session: Session, tools: list[dict], user_message: str, channel_id: str, stream: bool = False
    ) -> dict:
        user_msg = {"role": "user", "content": user_message}
        # API呼び出しが成功してから永続化する。失敗時にセッションに
        # ユーザーメッセージだけが残って不整合になるのを防ぐ。
        assistant_message = await self._chat_completions(session.messages + [user_msg], tools, channel_id, stream)
        await session.append_message(user_msg)
        await session.append_message(assistant_message)
        if tool_calls := assistant_message.get("tool_calls"):
            iteration = 0
            while tool_calls:
                if self.max_tool_iterations is not None and iteration >= self.max_tool_iterations:
                    logger.warning("tool call iteration limit reached (%s), stopping", self.max_tool_iterations)
                    break
                iteration += 1
                for tool_call in tool_calls:
                    await self.ui.send_tool_use(
                        channel_id,
                        tool_call["function"]["name"],
                        tool_call["function"]["arguments"],
                    )
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
                    await self.ui.send_tool_result(channel_id, tool_response)
                assistant_message = await self._chat_completions(session.messages, tools, channel_id, stream)
                await session.append_message(assistant_message)
                tool_calls = assistant_message.get("tool_calls")
        await self.ui.send(channel_id, assistant_message.get("content") or "(no assistant message)")
        return assistant_message

    async def assistant_turn_stateless(
        self, messages: list[dict], tools: list[dict], channel_id: str, stream: bool = False
    ) -> None:
        """セッション永続化なしでmessagesを直接LLMに渡して1ターン処理する。
        API UIから呼ばれる。クライアントが全履歴を管理するため、サーバー側は保存しない。
        """
        assistant_message = await self._chat_completions(messages, tools, channel_id, stream)
        current = list(messages) + [assistant_message]
        iteration = 0
        while tool_calls := assistant_message.get("tool_calls"):
            if self.max_tool_iterations is not None and iteration >= self.max_tool_iterations:
                logger.warning("tool call iteration limit reached (%s), stopping", self.max_tool_iterations)
                break
            iteration += 1
            for tool_call in tool_calls:
                await self.ui.send_tool_use(
                    channel_id,
                    tool_call["function"]["name"],
                    tool_call["function"]["arguments"],
                )
                try:
                    tool_args = json.loads(tool_call["function"]["arguments"])
                    tool_response = await self.call_tool(tool_call["function"]["name"], tool_args, channel_id)
                except json.JSONDecodeError:
                    tool_response = "tool_call.function.arguments JSON parse error"
                except Exception as e:
                    tool_response = f"error: {type(e).__name__}: {e}"
                current.append({
                    "role": "tool",
                    "content": tool_response,
                    "tool_call_id": tool_call["id"],
                })
            assistant_message = await self._chat_completions(current, tools, channel_id, stream)
            current.append(assistant_message)
        await self.ui.send(channel_id, assistant_message.get("content") or "(no assistant message)")
