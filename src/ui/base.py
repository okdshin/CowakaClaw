from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class IncomingMessage:
    content: str
    channel_id: str
    session_key: str


class UI(ABC):
    default_channel_id: str
    default_session_key: str

    @abstractmethod
    async def receive(self) -> IncomingMessage:
        """次のユーザーメッセージを待って返す"""

    @abstractmethod
    async def send(self, channel_id: str, text: str) -> None:
        """ユーザーにテキストを送る"""
