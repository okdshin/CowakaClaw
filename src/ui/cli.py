import asyncio
import sys

from .base import UI, IncomingMessage


async def read_stdin_line(prompt: str) -> str:
    """スレッドをブロックせずに stdin から1行読む。CancelledError に対応。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def on_readable() -> None:
        loop.remove_reader(sys.stdin.fileno())
        line = sys.stdin.readline()
        if not fut.done():
            if not line:  # EOF (Ctrl+D)
                fut.set_exception(EOFError("stdin closed"))
            else:
                fut.set_result(line.rstrip("\n"))

    sys.stdout.write(prompt)
    sys.stdout.flush()
    loop.add_reader(sys.stdin.fileno(), on_readable)
    try:
        return await fut
    except asyncio.CancelledError:
        loop.remove_reader(sys.stdin.fileno())
        raise


class CLI(UI):
    default_channel_id = "local"

    def __init__(self) -> None:
        # チャンクが1つ以上送られたチャンネルを記録する。
        # send() でストリーミング済みかどうかを判定するために使う。
        self._streamed: set[str] = set()

    async def receive(self) -> IncomingMessage:
        try:
            line = await read_stdin_line("> ")
        except EOFError:
            raise KeyboardInterrupt
        return IncomingMessage(
            content=line,
            channel_id="local",
            session_key="agent:main:cli:dm:local",
            stream=True,
        )

    async def send_stream_chunk(self, channel_id: str, chunk: str) -> None:
        self._streamed.add(channel_id)
        sys.stdout.write(chunk)
        sys.stdout.flush()

    async def send(self, channel_id: str, text: str) -> None:
        if channel_id in self._streamed:
            # ストリーミング済み: チャンクはすでに出力されているので改行だけ入れる
            self._streamed.discard(channel_id)
            print()
        else:
            print(text)
