import asyncio
import sys

from .base import IncomingMessage, UI


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

    async def receive(self) -> IncomingMessage:
        try:
            line = await read_stdin_line("> ")
        except EOFError:
            raise KeyboardInterrupt
        return IncomingMessage(
            content=line,
            channel_id="local",
            session_key="agent:main:cli:dm:local",
        )

    async def send(self, channel_id: str, text: str) -> None:
        print(text)
