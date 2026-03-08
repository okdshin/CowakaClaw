import asyncio
import os
import re

from .base import UI, IncomingMessage

try:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp
except ImportError as e:
    raise ImportError("slack_bolt is required for Slack UI. Install it with: pip install slack-bolt") from e


def strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


class Slack(UI):
    concurrent = True

    def __init__(self, default_channel_id: str):
        self.default_channel_id = default_channel_id
        self.queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        bot_token = os.environ.get("COWAKA_CLAW_SLACK_BOT_TOKEN")
        app_token = os.environ.get("COWAKA_CLAW_SLACK_APP_TOKEN")
        if bot_token is None:
            raise ValueError("COWAKA_CLAW_SLACK_BOT_TOKEN environment variable is not set")
        if app_token is None:
            raise ValueError("COWAKA_CLAW_SLACK_APP_TOKEN environment variable is not set")
        self.app = AsyncApp(token=bot_token)
        self.socket_handler = AsyncSocketModeHandler(self.app, app_token)
        self.register_handlers()

    def register_handlers(self) -> None:
        @self.app.event("app_mention")
        async def handle_app_mention(event) -> None:
            channel = event["channel"]
            thread_ts = event.get("thread_ts") or event["ts"]
            channel_id = f"{channel}|{thread_ts}"
            session_key = f"slack:thread:{channel}:{thread_ts}"
            await self.queue.put(IncomingMessage(
                content=strip_mention(event["text"]),
                channel_id=channel_id,
                session_key=session_key,
            ))

        @self.app.event("message")
        async def handle_dm(event) -> None:
            if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
                return
            channel = event["channel"]
            await self.queue.put(IncomingMessage(
                content=event["text"],
                channel_id=channel,
                session_key=f"slack:im:{channel}",
            ))

    async def receive(self) -> IncomingMessage:
        return await self.queue.get()

    async def send(self, channel_id: str, text: str) -> None:
        if "|" in channel_id:
            channel, thread_ts = channel_id.split("|", 1)
            await self.app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        else:
            await self.app.client.chat_postMessage(channel=channel_id, text=text)

    async def send_tool_result(self, channel_id: str, text: str) -> None:
        """Slackではツール結果を送信しない（中間結果のノイズを避ける）"""

    async def start(self) -> None:
        await self.socket_handler.start_async()
