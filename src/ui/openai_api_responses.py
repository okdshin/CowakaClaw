import asyncio
import json
import time
import uuid
from pathlib import Path

from .base import IncomingMessage, UI

try:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError as e:
    raise ImportError(
        "fastapi and uvicorn are required for OpenAI API Responses UI. "
        "Install them with: pip install fastapi uvicorn"
    ) from e


def _extract_user_content(input_val) -> str:
    """input フィールドから最後のユーザーメッセージのテキストを取り出す。"""
    if isinstance(input_val, str):
        return input_val
    last = ""
    for item in input_val:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            last = content
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"):
                    parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    parts.append(part)
            last = "".join(parts)
    return last


def _session_key(resp_id: str) -> str:
    return f"openai_api_responses:session:{resp_id}"


class _ResponseRequest(BaseModel):
    model: str
    input: str | list
    instructions: str | None = None  # 現在は無視。システムプロンプトはSOUL.md/MEMORY.mdから生成される。
    stream: bool = False
    previous_response_id: str | None = None


class OpenAIAPIResponses(UI):
    """OpenAI互換の Responses API を公開するUI。
    previous_response_id によるサーバーサイドセッション管理をサポートする。
    - previous_response_id なし: 新しいセッションを開始し、resp_id を発行する。
    - previous_response_id あり: 対応するセッションに対話を追記する。
    セッション履歴はエージェントのJSONLセッションファイルに永続化される。
    session_key は resp_id から決定論的に導出するため、別途マッピングファイルは不要。
    """

    concurrent = True
    default_channel_id = "openai_api_responses"

    def __init__(self, host: str = "0.0.0.0", port: int = 8000, base_dir: str | Path = "./base_dir"):
        self.host = host
        self.port = port
        self._sessions_json_path = Path(base_dir) / "agents" / "main" / "sessions" / "sessions.json"
        self._request_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        # 非ストリーミング: channel_id → 応答を待つFuture
        self._pending: dict[str, asyncio.Future[str]] = {}
        # ストリーミング: channel_id → チャンクテキストを流すQueue（Noneがsentinel）
        self._stream_queues: dict[str, asyncio.Queue[str | None]] = {}
        self._streaming_ids: set[str] = set()
        self.app = FastAPI(title="cowaka-claw Responses API")
        self._setup_routes()

    def _session_exists(self, session_key: str) -> bool:
        if not self._sessions_json_path.exists():
            return False
        with open(self._sessions_json_path) as f:
            sessions = json.load(f)
        return session_key in sessions

    def _setup_routes(self) -> None:
        @self.app.post("/v1/responses")
        async def create_response(req: _ResponseRequest):
            user_content = _extract_user_content(req.input)
            if not user_content:
                return JSONResponse(
                    status_code=400,
                    content={"error": {"message": "No user message found in input"}},
                )

            resp_id = f"resp_{uuid.uuid4().hex}"

            if req.previous_response_id:
                sk = _session_key(req.previous_response_id)
                exists = await asyncio.to_thread(self._session_exists, sk)
                if not exists:
                    return JSONResponse(
                        status_code=400,
                        content={"error": {"message": f"previous_response_id not found: {req.previous_response_id}"}},
                    )
            else:
                sk = _session_key(resp_id)

            if req.stream:
                return await self._handle_streaming(resp_id, sk, req.model, user_content)
            else:
                return await self._handle_non_streaming(resp_id, sk, req.model, user_content)

    async def _handle_non_streaming(self, resp_id: str, session_key: str, model: str, content: str):
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[resp_id] = future

        await self._request_queue.put(IncomingMessage(
            content=content,
            channel_id=resp_id,
            session_key=session_key,
        ))

        try:
            response_text = await future
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

        item_id = f"msg_{uuid.uuid4().hex[:24]}"
        return {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": response_text}],
                    "status": "completed",
                }
            ],
        }

    async def _handle_streaming(self, resp_id: str, session_key: str, model: str, content: str):
        stream_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._stream_queues[resp_id] = stream_queue
        self._streaming_ids.add(resp_id)

        await self._request_queue.put(IncomingMessage(
            content=content,
            channel_id=resp_id,
            session_key=session_key,
            stream=True,
        ))

        item_id = f"msg_{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        async def generate():
            yield sse("response.created", {
                "type": "response.created",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": created,
                    "model": model,
                    "status": "in_progress",
                    "output": [],
                },
            })
            yield sse("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "content": [],
                    "status": "in_progress",
                },
            })
            yield sse("response.content_part.added", {
                "type": "response.content_part.added",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            })

            full_text_parts: list[str] = []
            while True:
                chunk = await stream_queue.get()
                if chunk is None:
                    break
                full_text_parts.append(chunk)
                yield sse("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": chunk,
                })

            full_text = "".join(full_text_parts)
            yield sse("response.output_text.done", {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": full_text,
            })
            yield sse("response.content_part.done", {
                "type": "response.content_part.done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": full_text},
            })
            yield sse("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full_text}],
                    "status": "completed",
                },
            })
            yield sse("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": created,
                    "model": model,
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": item_id,
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": full_text}],
                            "status": "completed",
                        }
                    ],
                },
            })
            yield "event: done\ndata: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    async def receive(self) -> IncomingMessage:
        return await self._request_queue.get()

    async def send_stream_chunk(self, channel_id: str, chunk: str) -> None:
        q = self._stream_queues.get(channel_id)
        if q:
            await q.put(chunk)

    async def send(self, channel_id: str, text: str) -> None:
        if channel_id in self._streaming_ids:
            self._streaming_ids.discard(channel_id)
            q = self._stream_queues.pop(channel_id, None)
            if q:
                await q.put(None)
            return
        future = self._pending.pop(channel_id, None)
        if future and not future.done():
            future.set_result(text)

    async def send_tool_result(self, channel_id: str, text: str) -> None:
        """ツール中間結果はAPIレスポンスに含めない。"""

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()
