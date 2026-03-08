from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ui.slack import strip_mention


class TestStripMention:
    def test_strips_mention(self):
        assert strip_mention("<@U12345> hello") == "hello"

    def test_strips_mention_no_trailing_text(self):
        assert strip_mention("<@U12345>") == ""

    def test_no_mention(self):
        assert strip_mention("just text") == "just text"

    def test_multiple_mentions(self):
        assert strip_mention("<@U12345> <@U67890> hi") == "hi"

    def test_empty_string(self):
        assert strip_mention("") == ""

    def test_mention_in_middle_is_not_stripped(self):
        # メンションが先頭以外にある場合は re.sub で除去されるが、strip() で余分なスペースは消える
        result = strip_mention("hello <@U12345> world")
        assert "<@U12345>" not in result


def _make_slack(default_channel_id="C_DEFAULT"):
    """Slack UI をモックして初期化する。handlers dict にイベントハンドラーを収集する。"""
    handlers: dict = {}

    with patch.dict("os.environ", {
        "COWAKA_CLAW_SLACK_BOT_TOKEN": "xoxb-test",
        "COWAKA_CLAW_SLACK_APP_TOKEN": "xapp-test",
    }):
        mock_app = MagicMock()

        def event_side_effect(event_name):
            def decorator(fn):
                handlers[event_name] = fn
                return fn
            return decorator

        mock_app.event.side_effect = event_side_effect

        with patch("src.ui.slack.AsyncApp", return_value=mock_app), \
             patch("src.ui.slack.AsyncSocketModeHandler"):
            from src.ui.slack import Slack
            slack = Slack(default_channel_id=default_channel_id)

    return slack, handlers


class TestSlackHandlers:
    async def test_app_mention_queues_message(self):
        slack, handlers = _make_slack()
        event = {"channel": "C123", "ts": "1234567890.123456", "text": "<@U999> hello bot"}
        await handlers["app_mention"](event)
        msg = slack.queue.get_nowait()
        assert msg.content == "hello bot"
        assert msg.channel_id == "C123|1234567890.123456"
        assert msg.session_key == "slack:thread:C123:1234567890.123456"

    async def test_app_mention_uses_thread_ts_when_present(self):
        slack, handlers = _make_slack()
        event = {"channel": "C123", "ts": "111.000", "thread_ts": "999.000", "text": "<@U999> reply"}
        await handlers["app_mention"](event)
        msg = slack.queue.get_nowait()
        assert msg.channel_id == "C123|999.000"
        assert msg.session_key == "slack:thread:C123:999.000"

    async def test_dm_queues_message(self):
        slack, handlers = _make_slack()
        event = {"channel": "D123", "channel_type": "im", "text": "direct message"}
        await handlers["message"](event)
        msg = slack.queue.get_nowait()
        assert msg.content == "direct message"
        assert msg.channel_id == "D123"
        assert msg.session_key == "slack:im:D123"

    async def test_dm_ignores_non_im(self):
        slack, handlers = _make_slack()
        event = {"channel": "C123", "channel_type": "channel", "text": "public"}
        await handlers["message"](event)
        assert slack.queue.empty()

    async def test_dm_ignores_bot_messages(self):
        slack, handlers = _make_slack()
        event = {"channel": "D123", "channel_type": "im", "text": "bot msg", "bot_id": "B123"}
        await handlers["message"](event)
        assert slack.queue.empty()

    async def test_dm_ignores_subtype(self):
        slack, handlers = _make_slack()
        event = {"channel": "D123", "channel_type": "im", "text": "msg", "subtype": "file_share"}
        await handlers["message"](event)
        assert slack.queue.empty()


class TestSlackSend:
    async def test_send_with_thread_calls_post_with_thread_ts(self):
        slack, _ = _make_slack()
        slack.app.client.chat_postMessage = AsyncMock()
        await slack.send("C123|1234.567", "reply text")
        slack.app.client.chat_postMessage.assert_called_once_with(
            channel="C123", thread_ts="1234.567", text="reply text"
        )

    async def test_send_without_thread_calls_post_without_thread_ts(self):
        slack, _ = _make_slack()
        slack.app.client.chat_postMessage = AsyncMock()
        await slack.send("C123", "message")
        slack.app.client.chat_postMessage.assert_called_once_with(
            channel="C123", text="message"
        )

    async def test_send_tool_result_is_noop(self):
        slack, _ = _make_slack()
        slack.app.client.chat_postMessage = AsyncMock()
        await slack.send_tool_result("C123", "tool result")
        slack.app.client.chat_postMessage.assert_not_called()

    async def test_receive_returns_queued_message(self):
        from src.ui.base import IncomingMessage
        slack, _ = _make_slack()
        expected = IncomingMessage(content="hi", channel_id="C1", session_key="sk")
        await slack.queue.put(expected)
        msg = await slack.receive()
        assert msg is expected
