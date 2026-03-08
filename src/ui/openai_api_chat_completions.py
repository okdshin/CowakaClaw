import asyncio
import json
import time
import uuid

from .base import IncomingMessage, UI

try:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response
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
    # userフィールドをセッションキーとして使用する。
    # 同じuserからの同時リクエストはセッションロックでシリアライズされる。
    # 未指定の場合はリクエスト単位で独立したセッションキーを生成する。
    user: str | None = None


class OpenAIAPIChatCompletions(UI):
    """OpenAI互換のChat Completions APIを公開するUI。
    クライアントがmessages全体を送ってくる（ステートレス）ため、
    サーバー側ではセッションファイルへの永続化を行わない。
    """

    concurrent = True
    default_channel_id = "openai_api_chat_completions"

    def __init__(self, host: str = "0.0.0.0", port: int = 8000):
        self.host = host
        self.port = port
        self._request_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        # channel_id (= request_id) → 応答を待つFuture
        self._pending: dict[str, asyncio.Future[str]] = {}
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

            if req.stream:
                return JSONResponse(
                    status_code=400,
                    content={"error": {"message": "streaming is not supported"}},
                )

            # messages全体をdictに変換（None値は除外）
            messages_dicts = [
                {k: v for k, v in m.model_dump().items() if v is not None}
                for m in req.messages
            ]

            # セッションキー: userフィールド優先、なければリクエスト単位
            request_id = uuid.uuid4().hex
            if req.user:
                session_key = f"openai_api_chat_completions:user:{req.user}"
            else:
                session_key = f"openai_api_chat_completions:request:{request_id}"

            loop = asyncio.get_event_loop()
            future: asyncio.Future[str] = loop.create_future()
            self._pending[request_id] = future

            await self._request_queue.put(IncomingMessage(
                content=last.content or "",
                channel_id=request_id,
                session_key=session_key,
                messages_override=messages_dicts,
            ))

            try:
                response_text = await future
            except Exception as e:
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": str(e)}},
                )

            body = {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_text,
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
            return Response(
                content=json.dumps(body, ensure_ascii=False),
                media_type="application/json",
            )

    async def receive(self) -> IncomingMessage:
        return await self._request_queue.get()

    async def send(self, channel_id: str, text: str) -> None:
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
