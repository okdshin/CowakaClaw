import asyncio
import json
import time
import uuid

from .base import UI, IncomingMessage

try:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError as e:
    raise ImportError(
        "fastapi and uvicorn are required for OpenAI API Chat Completions UI. "
        "Install them with: pip install fastapi uvicorn"
    ) from e


class _ChatMessage(BaseModel):
    role: str
    content: str | list | None = None


class _ChatCompletionRequest(BaseModel):
    model: str
    messages: list[_ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class OpenAIAPIChatCompletions(UI):
    """OpenAI互換のChat Completions APIを公開するUI。
    クライアントがmessages全体を送ってくる（ステートレス）ため、
    サーバー側ではセッションファイルへの永続化を行わない。
    """

    concurrent = True
    supports_cron = False
    default_channel_id = "openai_api_chat_completions"

    def __init__(self, host: str = "0.0.0.0", port: int = 8000):
        self.host = host
        self.port = port
        self._request_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        # 非ストリーミング: channel_id → 応答を待つFuture
        self._pending: dict[str, asyncio.Future[str]] = {}
        # ストリーミング: channel_id → チャンクを流すQueue（Noneがsentinel）
        self._stream_queues: dict[str, asyncio.Queue[str | None]] = {}
        # ストリーミングリクエストのchannel_idとモデル名を管理
        self._streaming_ids: set[str] = set()
        self._request_models: dict[str, str] = {}
        self.app = FastAPI(title="cowaka-claw API")
        self._setup_routes()

    def _setup_routes(self) -> None:
        @self.app.post("/v1/chat/completions")
        async def chat_completions(req: _ChatCompletionRequest, raw: Request):
            if not req.messages:
                return JSONResponse(status_code=400, content={"error": {"message": "messages is empty"}})

            last = req.messages[-1]
            if last.role != "user":
                return JSONResponse(
                    status_code=400,
                    content={"error": {"message": "last message must be from user"}},
                )

            # messages全体をdictに変換（None値は除外）
            messages_dicts = [
                {k: v for k, v in m.model_dump().items() if v is not None}
                for m in req.messages
            ]

            request_id = uuid.uuid4().hex
            session_key = f"openai_api_chat_completions:request:{request_id}"

            if req.stream:
                return await self._handle_streaming(
                    request_id, session_key, req.model, last.content or "", messages_dicts
                )
            else:
                return await self._handle_non_streaming(
                    request_id, session_key, req.model, last.content or "", messages_dicts
                )

    async def _handle_non_streaming(
        self, request_id: str, session_key: str, model: str, content: str, messages_dicts: list[dict]
    ):
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[request_id] = future

        await self._request_queue.put(IncomingMessage(
            content=content,
            channel_id=request_id,
            session_key=session_key,
            messages_override=messages_dicts,
            stream=False,
        ))

        try:
            response_text = await future
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

        return {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop",
                }
            ],
        }

    async def _handle_streaming(
        self, request_id: str, session_key: str, model: str, content: str, messages_dicts: list[dict]
    ):
        stream_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._stream_queues[request_id] = stream_queue
        self._streaming_ids.add(request_id)
        self._request_models[request_id] = model

        await self._request_queue.put(IncomingMessage(
            content=content,
            channel_id=request_id,
            session_key=session_key,
            messages_override=messages_dicts,
            stream=True,
        ))

        chunk_id = f"chatcmpl-{request_id}"
        created = int(time.time())

        def make_chunk(delta: dict, finish_reason: str | None = None) -> str:
            data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(data)}\n\n"

        async def generate():
            yield make_chunk({"role": "assistant"})
            while True:
                item = await stream_queue.get()
                if item is None:
                    yield make_chunk({}, finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    break
                yield item

        return StreamingResponse(generate(), media_type="text/event-stream")

    async def receive(self) -> IncomingMessage:
        return await self._request_queue.get()

    async def send_stream_chunk(self, channel_id: str, chunk: str) -> None:
        q = self._stream_queues.get(channel_id)
        if not q:
            return
        model = self._request_models.get(channel_id, "unknown")
        created = int(time.time())
        data = {
            "id": f"chatcmpl-{channel_id}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        }
        await q.put(f"data: {json.dumps(data)}\n\n")

    async def send(self, channel_id: str, text: str) -> None:
        if channel_id in self._streaming_ids:
            # ストリーミング完了: sentinelを送ってSSEストリームを閉じる
            self._streaming_ids.discard(channel_id)
            self._request_models.pop(channel_id, None)
            q = self._stream_queues.pop(channel_id, None)
            if q:
                await q.put(None)
            return
        # 非ストリーミング: Futureを完了させる
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
