from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class IncomingMessage:
    content: str
    channel_id: str
    session_key: str


class UI(ABC):
    default_channel_id: str
    concurrent: bool = False  # Trueにすると複数メッセージを並列処理する

    async def start(self) -> None:
        """エージェント起動時に呼ばれる。必要に応じてオーバーライドする。"""

    @abstractmethod
    async def receive(self) -> IncomingMessage:
        """次のユーザーメッセージを待って返す"""

    @abstractmethod
    async def send(self, channel_id: str, text: str) -> None:
        """ユーザーにテキストを送る"""

    async def send_tool_result(self, channel_id: str, text: str) -> None:
        """ツール呼び出し結果を送る。デフォルトは send と同じ。
        ノイズを抑えたいUIはオーバーライドして何もしないようにする。"""
        await self.send(channel_id, text)
