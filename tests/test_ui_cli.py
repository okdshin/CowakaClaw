import pytest

from src.ui.cli import CLI


class TestCLISend:
    async def test_send_prints_text(self, capsys):
        cli = CLI()
        await cli.send("ch", "hello world")
        assert capsys.readouterr().out == "hello world\n"

    async def test_send_after_stream_chunk_prints_newline_only(self, capsys):
        cli = CLI()
        await cli.send_stream_chunk("ch", "chunk")
        capsys.readouterr()  # チャンク出力をクリア
        await cli.send("ch", "ignored")
        assert capsys.readouterr().out == "\n"

    async def test_send_clears_streamed_flag(self, capsys):
        cli = CLI()
        await cli.send_stream_chunk("ch", "x")
        await cli.send("ch", "first")
        capsys.readouterr()
        # 2回目の send はストリームフラグが消えているのでテキストを出力する
        await cli.send("ch", "second")
        assert capsys.readouterr().out == "second\n"

    async def test_send_different_channels_independent(self, capsys):
        cli = CLI()
        await cli.send_stream_chunk("ch1", "x")
        # ch2 はストリームなし → テキストを出力
        await cli.send("ch2", "text for ch2")
        assert "text for ch2" in capsys.readouterr().out

    async def test_send_stream_chunk_writes_to_stdout(self, capsys):
        cli = CLI()
        await cli.send_stream_chunk("ch", "hello")
        assert capsys.readouterr().out == "hello"

    async def test_send_stream_chunk_multiple_chunks_accumulated(self, capsys):
        cli = CLI()
        await cli.send_stream_chunk("ch", "foo")
        await cli.send_stream_chunk("ch", "bar")
        assert capsys.readouterr().out == "foobar"

    async def test_send_tool_result_prints_text(self, capsys):
        # デフォルト実装は send と同じ
        cli = CLI()
        await cli.send_tool_result("ch", "tool output")
        assert capsys.readouterr().out == "tool output\n"
