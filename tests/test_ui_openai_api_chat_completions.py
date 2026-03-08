"""OpenAIAPIChatCompletions UI のユニットテスト。

send/receive の内部ロジックと HTTP エンドポイントを検証する。
HTTP テストは httpx がインストールされていない場合はスキップする。
"""

import asyncio
import json

import pytest

from src.ui.openai_api_chat_completions import OpenAIAPIChatCompletions

httpx = pytest.importorskip("httpx")


# ---------------------------------------------------------------------------
# send / send_stream_chunk / receive の内部ロジック
# ---------------------------------------------------------------------------


class TestSendReceive:
    async def test_receive_returns_queued_message(self):
        ui = OpenAIAPIChatCompletions()
        from src.ui.base import IncomingMessage
        msg = IncomingMessage(content="hi", channel_id="ch", session_key="sk")
        await ui._request_queue.put(msg)
        received = await ui.receive()
        assert received is msg

    async def test_send_resolves_pending_future(self):
        ui = OpenAIAPIChatCompletions()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        ui._pending["ch"] = future
        await ui.send("ch", "response text")
        assert future.done()
        assert future.result() == "response text"

    async def test_send_removes_pending_entry(self):
        ui = OpenAIAPIChatCompletions()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        ui._pending["ch"] = future
        await ui.send("ch", "result")
        assert "ch" not in ui._pending

    async def test_send_ignores_unknown_channel(self):
        # 存在しない channel_id への send は例外を投げない
        ui = OpenAIAPIChatCompletions()
        await ui.send("unknown", "text")

    async def test_send_streaming_puts_sentinel_and_cleans_up(self):
        ui = OpenAIAPIChatCompletions()
        q: asyncio.Queue[str | None] = asyncio.Queue()
        ui._stream_queues["ch"] = q
        ui._streaming_ids.add("ch")
        ui._request_models["ch"] = "gpt-4o"
        await ui.send("ch", "done")
        assert q.get_nowait() is None
        assert "ch" not in ui._streaming_ids
        assert "ch" not in ui._stream_queues
        assert "ch" not in ui._request_models

    async def test_send_stream_chunk_puts_sse_chunk(self):
        ui = OpenAIAPIChatCompletions()
        q: asyncio.Queue[str | None] = asyncio.Queue()
        ui._stream_queues["ch"] = q
        ui._request_models["ch"] = "gpt-4o"
        await ui.send_stream_chunk("ch", "hello")
        item = q.get_nowait()
        assert item is not None
        assert item.startswith("data: ")
        data = json.loads(item.removeprefix("data: ").strip())
        assert data["choices"][0]["delta"]["content"] == "hello"

    async def test_send_stream_chunk_unknown_channel_is_noop(self):
        ui = OpenAIAPIChatCompletions()
        await ui.send_stream_chunk("unknown", "hello")  # 例外を投げない

    async def test_send_tool_result_is_noop(self):
        ui = OpenAIAPIChatCompletions()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        ui._pending["ch"] = future
        await ui.send_tool_result("ch", "tool result")
        # send_tool_result は何もしない → future は未解決のまま
        assert not future.done()


# ---------------------------------------------------------------------------
# HTTP エンドポイント（httpx ASGI）
# ---------------------------------------------------------------------------


def _make_request(messages=None, stream=False):
    return {
        "model": "gpt-4o",
        "messages": messages or [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


class TestHTTPNonStreaming:
    async def _post(self, client, ui, payload, response_text="agent reply"):
        """リクエストを送って、バックグラウンドでエージェントが応答するシミュレーション。"""
        async def fake_agent():
            msg = await ui.receive()
            await ui.send(msg.channel_id, response_text)

        task = asyncio.create_task(fake_agent())
        response = await client.post("/v1/chat/completions", json=payload)
        await task
        return response

    async def test_basic_request(self):
        ui = OpenAIAPIChatCompletions()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            response = await self._post(client, ui, _make_request())
        assert response.status_code == 200
        data = response.json()
        assert data["choices"][0]["message"]["content"] == "agent reply"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["object"] == "chat.completion"

    async def test_messages_override_contains_all_messages(self):
        """IncomingMessage.messages_override に全メッセージが入ること。"""
        ui = OpenAIAPIChatCompletions()
        received_msg = None

        async def fake_agent():
            nonlocal received_msg
            received_msg = await ui.receive()
            await ui.send(received_msg.channel_id, "ok")

        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            await client.post("/v1/chat/completions", json=_make_request(messages=messages))
            await task

        assert received_msg is not None
        assert received_msg.messages_override is not None
        assert len(received_msg.messages_override) == 3

    async def test_generates_unique_session_key_per_request(self):
        ui = OpenAIAPIChatCompletions()
        keys = []

        async def fake_agent():
            for _ in range(2):
                msg = await ui.receive()
                keys.append(msg.session_key)
                await ui.send(msg.channel_id, "ok")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            await client.post("/v1/chat/completions", json=_make_request())
            await client.post("/v1/chat/completions", json=_make_request())
            await task

        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert all(k.startswith("openai_api_chat_completions:request:") for k in keys)

    async def test_empty_messages_returns_400(self):
        ui = OpenAIAPIChatCompletions()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions", json={"model": "gpt-4o", "messages": []}
            )
        assert response.status_code == 400

    async def test_last_message_not_user_returns_400(self):
        ui = OpenAIAPIChatCompletions()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=_make_request(messages=[{"role": "assistant", "content": "hi"}]),
            )
        assert response.status_code == 400


class TestHTTPStreaming:
    async def test_streaming_response_is_sse(self):
        ui = OpenAIAPIChatCompletions()

        async def fake_agent():
            msg = await ui.receive()
            await ui.send_stream_chunk(msg.channel_id, "hello ")
            await ui.send_stream_chunk(msg.channel_id, "world")
            await ui.send(msg.channel_id, "")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            response = await client.post(
                "/v1/chat/completions", json=_make_request(stream=True)
            )
            await task

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        lines = response.text.splitlines()
        data_lines = [l for l in lines if l.startswith("data: ") and l != "data: [DONE]"]
        chunks = [json.loads(l.removeprefix("data: ")) for l in data_lines]

        # 最初のチャンクはroleのみ
        assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"

        # テキストチャンク
        text_chunks = [
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c["choices"][0]["delta"].get("content")
        ]
        assert "".join(text_chunks) == "hello world"

        # 最後は finish_reason=stop
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert "data: [DONE]" in response.text
