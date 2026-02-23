import asyncio
from datetime import datetime
import os
from collections.abc import AsyncGenerator
import json
from pathlib import Path
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

import openai
from openai import pydantic_function_tool
from pydantic import BaseModel, Field
import mcp
from mcp.client.stdio import stdio_client
from croniter import croniter


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


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


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
            session_id = timestamp()
            sessions[session_key] = {
                "sessionId": session_id,
                "updatedAt": datetime.now().astimezone().isoformat(),
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


class MemoryUpdate(BaseModel):
    """
    Update MEMORY.md to persist important information across sessions.
    Call this when:
    - User shares personal facts, preferences, or goals
    - A decision or conclusion is reached
    - An ongoing project or task is established
    - User corrects previously stored information (use mode='replace')
    Do NOT call for temporary or session-specific information.
    """
    section: str = Field(..., description="Markdown heading to update (e.g. 'User Preferences')")
    content: str = Field(..., description="Content to write under the section")
    mode: Literal["append", "replace"] = Field("append", description="'append' or 'replace'")


def call_memory_update(
    workspace_path: Path,
    section: str,
    content: str,
    mode: str = "append",
) -> str:
    memory_path = workspace_path / "MEMORY.md"
    if memory_path.exists():
        with open(memory_path) as f:
            current = f.read()
    else:
        current = ""

    heading = f"## {section}"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_entry = f"{content}\n<!-- updated: {timestamp} -->"

    if heading in current:
        # セクションを見つけて処理
        before, _, rest = current.partition(heading)
        # 次のセクション（##）までを切り出す
        parts = rest.split("\n## ", 1)
        section_body = parts[0]
        after = ("\n## " + parts[1]) if len(parts) > 1 else ""

        if mode == "replace":
            new_section = f"{heading}\n{new_entry}"
        else:  # append
            new_section = f"{heading}{section_body}\n{new_entry}"

        updated = before + new_section + after
    else:
        # セクションが存在しない → 末尾に追加
        updated = current.rstrip() + f"\n\n{heading}\n{new_entry}\n"

    memory_path.write_text(updated, encoding="utf-8")
    return f"MEMORY.md updated: section='{section}', mode={mode}"


class AddCronJob(BaseModel):
    """
    Create cron job
    """


class CronJobManager:
    def __init__(self, base_dir_path: Path):
        self.base_dir_path = base_dir_path
        self.jobs_path = base_dir_path / "cron" / "jobs.json"
        self.tasks: dict[str, asyncio.Task] = {}  # job_id → 実行中のTask

    def load_jobs(self) -> dict:
        if self.jobs_path.exists():
            with open(self.jobs_path) as f:
                return json.load(f)
        return {}

    def save_jobs(self, jobs: dict) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jobs_path, "w") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)

    def add_cron_job(
        self,
        job_id: str,
        job_type: Literal["at", "cron", "every"],
        message: str,
    ) -> None:
        jobs = self._load_jobs()
        jobs[job_id] = {"job_type": job_type, "created_at": datetime.now().isoformat()}
        self.save_jobs(jobs)

    def delete_cron_job(self, job_id: str) -> None:
        jobs = self.load_jobs()
        jobs.pop(job_id, None)
        self.save_jobs(jobs)
        # 実行中タスクもキャンセル
        if task := self.tasks.pop(job_id, None):
            task.cancel()

    async def scheduler_loop(self, run_cron_job_fn) -> None:
        """全ジョブを監視し、未起動のジョブをタスクとして起動する"""
        while True:
            jobs = self.load_jobs()
            for job_id, job in jobs.items():
                if job_id not in self.tasks or self.tasks[job_id].done():
                    self.tasks[job_id] = asyncio.create_task(
                        self.job_task(job_id, job["type"], job["schedule"], job["message"], run_cron_job_fn)
                    )
            # 削除されたジョブのタスクをキャンセル
            for job_id in list(self.tasks):
                if job_id not in jobs:
                    self.tasks.pop(job_id).cancel()
            await asyncio.sleep(60)  # 1分ごとにjobs.jsonを再チェック

    async def job_task(
        self,
        job_id: str,
        job_type: Literal["at", "cron", "every"],
        when: str,
        message: str,
        run_fn,
    ) -> None:
        try:
            if job_type == "at":
                await self.run_at(job_id, when, message, run_fn)
            elif job_type == "cron":
                await self.run_cron(job_id, when, message, run_fn)
            elif job_type == "every":
                await self.run_every(job_id, when, message, run_fn)
            else:
                print(f"[cron:{job_id}] unknown type: {job_type}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[cron:{job_id}] error: {e}")

    async def run_at(self, job_id: str, when: str, message: str, run_fn) -> None:
        """単発: 絶対時刻 or 相対時間"""
        # "20m", "2h" のような相対指定を処理
        if not when[0].isdigit() is False and when[-1] in ("m", "h", "s"):
            run_at = self.parse_relative(when)
        else:
            run_at = datetime.fromisoformat(when)

        wait_sec = (run_at - datetime.now()).total_seconds()
        if wait_sec > 0:
            print(f"[cron:{job_id}] at → {run_at.strftime('%Y-%m-%d %H:%M:%S')} ({wait_sec:.0f}s)")
            await asyncio.sleep(wait_sec)
            await run_fn(job_id, message)
        self.delete_cron_job(job_id)

    async def run_cron(self, job_id: str, when: str, message: str, run_fn) -> None:
        """繰り返し: cron式"""
        while True:
            now = datetime.now().astimezone()
            next_run = croniter(when, now).get_next(datetime)
            wait_sec = (next_run - now).total_seconds()
            print(f"[cron:{job_id}] next → {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_sec:.0f}s)")
            await asyncio.sleep(wait_sec)
            await run_fn(job_id, message)

    async def run_every(self, job_id: str, when: str, message: str, run_fn) -> None:
        """繰り返し: 固定インターバル（lastRunAtを基準にドリフト防止）"""
        jobs = self.load_jobs()
        last_run_at = jobs[job_id].get("last_run_at")

        # 前回実行時刻から次回を計算（再起動後もドリフトしない）
        if last_run_at:
            next_run = datetime.fromisoformat(last_run_at) + \
                       datetime.timedelta(seconds=when)
            wait_sec = max(0, (next_run - datetime.now()).total_seconds())
        else:
            wait_sec = when

        while True:
            print(f"[cron:{job_id}] every {when}s → next in {wait_sec:.0f}s")
            await asyncio.sleep(wait_sec)
            await run_fn(job_id, message)

            # last_run_at を更新
            jobs = self._load_jobs()
            if job_id in jobs:
                jobs[job_id]["last_run_at"] = datetime.now().astimezone().isoformat()
                self._save_jobs(jobs)

            wait_sec = when  # 以降は固定インターバル

    @staticmethod
    def parse_when(when: str | int) -> datetime | datetime.timedelta:
        # ミリ秒のUnixタイムスタンプ（int or 数字文字列）
        if isinstance(when, int) or (isinstance(when, str) and when.isdigit()):
            return datetime.fromtimestamp(int(when) / 1000)
        # 相対時間: "20m", "2h", "30s"
        if isinstance(when, str) and when[-1] in ("m", "h", "s") and when[:-1].isdigit():
            return parse_duration(when)  # timedelta を返す
        # ISO文字列: "2026-02-24T15:00:00"
        return datetime.fromisoformat(when)

    @staticmethod
    def parse_duration(s: str) -> datetime:
        """'20m', '2h', '30s' → datetime"""
        import datetime as dt
        unit = s[-1]
        value = int(s[:-1])
        delta = {"m": dt.timedelta(minutes=value),
                 "h": dt.timedelta(hours=value),
                 "s": dt.timedelta(seconds=value)}[unit]
        return datetime.now() + delta


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


async def main():
    async with CowakaClawAgent(
        model="openai-gpt-oss-20b",
        base_dir_path="./base_dir",
        workspace_path="./workspace",
        mcp_config_json_path="./mcp_config.json",
    ) as agent:
        await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
