"""OpenAIAPIResponses UI のユニットテスト。

_extract_user_content の純粋関数テスト、send/receive の内部ロジック、
HTTP エンドポイントの動作を検証する。
HTTP テストは httpx がインストールされていない場合はスキップする。
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from src.ui.openai_api_responses import OpenAIAPIResponses, _extract_user_content

httpx = pytest.importorskip("httpx")


# ---------------------------------------------------------------------------
# _extract_user_content（純粋関数）
# ---------------------------------------------------------------------------


class TestExtractUserContent:
    def test_string_input(self):
        assert _extract_user_content("hello") == "hello"

    def test_list_with_user_message(self):
        messages = [{"role": "user", "content": "hello"}]
        assert _extract_user_content(messages) == "hello"

    def test_list_returns_last_user_message(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "last"},
        ]
        assert _extract_user_content(messages) == "last"

    def test_list_ignores_assistant_messages(self):
        messages = [{"role": "assistant", "content": "assistant text"}]
        assert _extract_user_content(messages) == ""

    def test_content_as_list_of_parts(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hello "},
                    {"type": "input_text", "text": "world"},
                ],
            }
        ]
        assert _extract_user_content(messages) == "hello world"

    def test_content_as_list_with_output_text(self):
        messages = [
            {"role": "user", "content": [{"type": "output_text", "text": "output"}]}
        ]
        assert _extract_user_content(messages) == "output"

    def test_empty_list(self):
        assert _extract_user_content([]) == ""

    def test_empty_string(self):
        assert _extract_user_content("") == ""


# ---------------------------------------------------------------------------
# send / send_stream_chunk / receive の内部ロジック
# ---------------------------------------------------------------------------


def _make_ui():
    with tempfile.TemporaryDirectory() as d:
        ui = OpenAIAPIResponses(base_dir=d)
    return ui


class TestSendReceive:
    async def test_receive_returns_queued_message(self):
        ui = _make_ui()
        from src.ui.base import IncomingMessage
        msg = IncomingMessage(content="hi", channel_id="ch", session_key="sk")
        await ui._request_queue.put(msg)
        received = await ui.receive()
        assert received is msg

    async def test_send_resolves_pending_future(self):
        ui = _make_ui()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        ui._pending["ch"] = future
        await ui.send("ch", "response text")
        assert future.done()
        assert future.result() == "response text"

    async def test_send_removes_pending_entry(self):
        ui = _make_ui()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        ui._pending["ch"] = future
        await ui.send("ch", "result")
        assert "ch" not in ui._pending

    async def test_send_ignores_unknown_channel(self):
        ui = _make_ui()
        await ui.send("unknown", "text")

    async def test_send_streaming_puts_sentinel_and_cleans_up(self):
        ui = _make_ui()
        q: asyncio.Queue[str | None] = asyncio.Queue()
        ui._stream_queues["ch"] = q
        ui._streaming_ids.add("ch")
        await ui.send("ch", "done")
        assert q.get_nowait() is None
        assert "ch" not in ui._streaming_ids
        assert "ch" not in ui._stream_queues

    async def test_send_stream_chunk_puts_chunk(self):
        ui = _make_ui()
        q: asyncio.Queue[str | None] = asyncio.Queue()
        ui._stream_queues["ch"] = q
        await ui.send_stream_chunk("ch", "hello")
        assert q.get_nowait() == "hello"

    async def test_send_stream_chunk_unknown_channel_is_noop(self):
        ui = _make_ui()
        await ui.send_stream_chunk("unknown", "hello")

    async def test_send_tool_result_is_noop(self):
        ui = _make_ui()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        ui._pending["ch"] = future
        await ui.send_tool_result("ch", "tool result")
        assert not future.done()


# ---------------------------------------------------------------------------
# HTTP エンドポイント（httpx ASGI）
# ---------------------------------------------------------------------------


def _make_request(input_val="hello", stream=False, previous_response_id=None):
    req = {"model": "gpt-4o", "input": input_val, "stream": stream}
    if previous_response_id:
        req["previous_response_id"] = previous_response_id
    return req


class TestHTTPNonStreaming:
    async def test_basic_request(self):
        ui = _make_ui()

        async def fake_agent():
            msg = await ui.receive()
            await ui.send(msg.channel_id, "hello from agent")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            response = await client.post("/v1/responses", json=_make_request())
            await task

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "response"
        assert data["status"] == "completed"
        output = data["output"][0]
        assert output["role"] == "assistant"
        assert output["content"][0]["text"] == "hello from agent"

    async def test_response_id_is_returned(self):
        ui = _make_ui()

        async def fake_agent():
            msg = await ui.receive()
            await ui.send(msg.channel_id, "ok")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            response = await client.post("/v1/responses", json=_make_request())
            await task

        data = response.json()
        assert data["id"].startswith("resp_")

    async def test_empty_input_returns_400(self):
        ui = _make_ui()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/responses", json=_make_request(input_val=""))
        assert response.status_code == 400

    async def test_previous_response_id_not_found_returns_400(self):
        ui = _make_ui()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/responses",
                json=_make_request(previous_response_id="resp_nonexistent"),
            )
        assert response.status_code == 400

    async def test_new_session_uses_new_resp_id_as_session_key(self):
        """previous_response_id なし → session_key は新しい resp_id から導出される。"""
        ui = _make_ui()
        received_msg = None

        async def fake_agent():
            nonlocal received_msg
            received_msg = await ui.receive()
            await ui.send(received_msg.channel_id, "ok")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            response = await client.post("/v1/responses", json=_make_request())
            await task

        resp_id = response.json()["id"]
        assert received_msg is not None
        assert received_msg.session_key == f"openai_api_responses:session:{resp_id}"

    async def test_list_input_extracts_user_content(self):
        ui = _make_ui()
        received_msg = None

        async def fake_agent():
            nonlocal received_msg
            received_msg = await ui.receive()
            await ui.send(received_msg.channel_id, "ok")

        messages = [{"role": "user", "content": "list input text"}]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            await client.post("/v1/responses", json=_make_request(input_val=messages))
            await task

        assert received_msg is not None
        assert received_msg.content == "list input text"


class TestHTTPStreaming:
    async def test_streaming_response_contains_sse_events(self):
        ui = _make_ui()

        async def fake_agent():
            msg = await ui.receive()
            await ui.send_stream_chunk(msg.channel_id, "hello ")
            await ui.send_stream_chunk(msg.channel_id, "world")
            await ui.send(msg.channel_id, "")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ui.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(fake_agent())
            response = await client.post("/v1/responses", json=_make_request(stream=True))
            await task

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # SSE イベントを解析
        events = {}
        for line in response.text.splitlines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
            elif line.startswith("data: ") and line != "data: [DONE]":
                events[current_event] = json.loads(line.removeprefix("data: "))

        assert "response.created" in events
        assert events["response.created"]["response"]["status"] == "in_progress"
        assert "response.output_text.done" in events
        assert events["response.output_text.done"]["text"] == "hello world"
        assert "response.completed" in events
        assert events["response.completed"]["response"]["status"] == "completed"
        output_text = events["response.completed"]["response"]["output"][0]["content"][0]["text"]
        assert output_text == "hello world"
