import asyncio
import json

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, RichLog, Static

from .base import UI, IncomingMessage


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ChatApp(App):
    """cowaka-claw – Claude-Code-style TUI"""

    CSS = """
    Screen {
        background: $background;
        layers: base;
    }

    #log {
        height: 1fr;
        padding: 0 1;
        background: $background;
        scrollbar-gutter: stable;
        border: none;
    }

    #statusbar {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }

    #user-input {
        dock: bottom;
        border: tall $primary;
    }
    """

    TITLE = "cowaka-claw"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("ctrl+l", "clear_log", "Clear", show=True),
    ]

    def __init__(self, input_queue: "asyncio.Queue[str]", **kwargs) -> None:
        super().__init__(**kwargs)
        self._input_queue = input_queue
        self._stream_buffer = ""
        self._busy = False
        self._busy_label = ""
        self._spinner_idx = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="log", markup=True, wrap=True, highlight=False)
        yield Static("[dim]Ready[/dim]", id="statusbar")
        yield Input(
            placeholder="Message cowaka-claw  ·  /new to reset session",
            id="user-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#user-input", Input).focus()
        self.set_interval(1 / 10, self._tick_spinner)

    def _tick_spinner(self) -> None:
        if not self._busy:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
        frame = _SPINNER[self._spinner_idx]
        self.query_one("#statusbar", Static).update(
            f"[cyan]{frame}[/cyan] [dim]{self._busy_label}[/dim]"
        )

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def _set_status(self, msg: str, busy: bool = False) -> None:
        self._busy = busy
        self._busy_label = msg
        if not busy:
            self.query_one("#statusbar", Static).update(f"[dim]{msg}[/dim]")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        log = self.query_one("#log", RichLog)
        for line in text.splitlines() or [""]:
            log.write(f"[bold green]>[/bold green] {line}")
        log.write("")
        self._set_status("Waiting...", busy=True)
        await self._input_queue.put(text)

    # ── display methods ──────────────────────────────────────────────────────

    def append_message(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        for line in (text or "").splitlines() or [""]:
            log.write(f"  {line}")
        log.write("")
        self._set_status("Ready")

    def append_tool_use(self, tool_name: str, tool_args_json: str) -> None:
        log = self.query_one("#log", RichLog)
        try:
            args = json.loads(tool_args_json)
            parts = [f"{k}={repr(v)[:50]}" for k, v in args.items()]
            args_str = ", ".join(parts)
        except Exception:
            args_str = (tool_args_json or "")[:80]
        log.write(f"[bold yellow]⚙ {tool_name}[/bold yellow]")
        if args_str:
            log.write(f"  [dim]{args_str}[/dim]")
        self._set_status(tool_name, busy=True)

    def append_tool_result(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        lines = (text or "").strip().splitlines()
        first = lines[0][:100] if lines else ""
        suffix = " …" if (len(lines) > 1 or len(text) > 100) else ""
        log.write(f"  [dim]↳ {first}{suffix}[/dim]\n")
        self._set_status("Thinking...", busy=True)

    def append_system(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(f"\n[bold blue]● {text}[/bold blue]\n")
        self._set_status("Ready")

    def start_streaming(self, chunk: str) -> None:
        log = self.query_one("#log", RichLog)
        self._stream_buffer += chunk
        # Flush complete lines into the log as they arrive
        while "\n" in self._stream_buffer:
            line, self._stream_buffer = self._stream_buffer.split("\n", 1)
            log.write(f"  {line}")
        self._set_status("Streaming...", busy=True)

    def finish_streaming(self) -> None:
        log = self.query_one("#log", RichLog)
        if self._stream_buffer:
            log.write(f"  {self._stream_buffer}")
        self._stream_buffer = ""
        log.write("")
        self._set_status("Ready")


class TextualUI(UI):
    """Textual-based TUI adapter."""

    default_channel_id = "local"
    supports_cron = False

    def __init__(self) -> None:
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._app = ChatApp(self._input_queue)
        self._is_streaming = False

    async def start(self) -> None:
        await self._app.run_async()

    async def receive(self) -> IncomingMessage:
        text = await self._input_queue.get()
        return IncomingMessage(
            content=text,
            channel_id="local",
            session_key="agent:main:textual:dm:local",
            stream=True,
        )

    async def send_stream_chunk(self, channel_id: str, chunk: str) -> None:
        self._is_streaming = True
        self._app.start_streaming(chunk)

    async def send(self, channel_id: str, text: str) -> None:
        if self._is_streaming:
            self._is_streaming = False
            self._app.finish_streaming()
        else:
            self._app.append_message(text)

    async def send_tool_use(self, channel_id: str, tool_name: str, tool_args_json: str) -> None:
        self._app.append_tool_use(tool_name, tool_args_json)

    async def send_tool_result(self, channel_id: str, text: str) -> None:
        self._app.append_tool_result(text)
