import json

import pytest

from src.session.session import Session, _new_session_id


class TestNewSessionId:
    def test_format_has_three_parts(self):
        sid = _new_session_id()
        parts = sid.split("-")
        assert len(parts) == 3

    def test_date_part_length(self):
        sid = _new_session_id()
        date_part = sid.split("-")[0]
        assert len(date_part) == 8  # YYYYMMDD

    def test_time_part_length(self):
        sid = _new_session_id()
        time_part = sid.split("-")[1]
        assert len(time_part) == 6  # HHMMSS

    def test_hex_part_length(self):
        sid = _new_session_id()
        hex_part = sid.split("-")[2]
        assert len(hex_part) == 8

    def test_uniqueness(self):
        ids = {_new_session_id() for _ in range(20)}
        assert len(ids) == 20


class TestSessionLoad:
    @pytest.mark.asyncio
    async def test_new_session_key_creates_session(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        assert session.session_key == "test:key"
        assert session.messages == []

    @pytest.mark.asyncio
    async def test_sessions_json_created(self, tmp_path):
        await Session.load(tmp_path, "test:key")
        assert (tmp_path / "sessions.json").exists()

    @pytest.mark.asyncio
    async def test_sessions_json_contains_entry(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        with open(tmp_path / "sessions.json") as f:
            sessions = json.load(f)
        assert "test:key" in sessions
        assert sessions["test:key"]["sessionId"] == session.session_id

    @pytest.mark.asyncio
    async def test_same_key_returns_same_session_id(self, tmp_path):
        s1 = await Session.load(tmp_path, "test:key")
        s2 = await Session.load(tmp_path, "test:key")
        assert s1.session_id == s2.session_id

    @pytest.mark.asyncio
    async def test_different_keys_get_different_ids(self, tmp_path):
        s1 = await Session.load(tmp_path, "key:a")
        s2 = await Session.load(tmp_path, "key:b")
        assert s1.session_id != s2.session_id

    @pytest.mark.asyncio
    async def test_messages_restored_across_loads(self, tmp_path):
        s1 = await Session.load(tmp_path, "test:key")
        await s1.append_message({"role": "user", "content": "hello"})
        s2 = await Session.load(tmp_path, "test:key")
        assert len(s2.messages) == 1
        assert s2.messages[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_multiple_messages_restored(self, tmp_path):
        s1 = await Session.load(tmp_path, "test:key")
        await s1.append_message({"role": "user", "content": "msg1"})
        await s1.append_message({"role": "assistant", "content": "msg2"})
        s2 = await Session.load(tmp_path, "test:key")
        assert len(s2.messages) == 2

    @pytest.mark.asyncio
    async def test_creates_subdirectory(self, tmp_path):
        nested = tmp_path / "sub" / "dir"
        await Session.load(nested, "test:key")
        assert nested.exists()


class TestSessionAppendMessage:
    @pytest.mark.asyncio
    async def test_message_appended_to_memory(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "hi"})
        assert session.messages == [{"role": "user", "content": "hi"}]

    @pytest.mark.asyncio
    async def test_message_written_to_jsonl(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "hi"})
        lines = session.session_jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"role": "user", "content": "hi"}

    @pytest.mark.asyncio
    async def test_multiple_messages_all_in_jsonl(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "msg1"})
        await session.append_message({"role": "assistant", "content": "msg2"})
        lines = session.session_jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_append_updates_sessions_json(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "hi"})
        with open(tmp_path / "sessions.json") as f:
            sessions = json.load(f)
        assert "updatedAt" in sessions["test:key"]

    @pytest.mark.asyncio
    async def test_unicode_message_written(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "日本語テスト"})
        line = session.session_jsonl_path.read_text(encoding="utf-8").strip()
        assert json.loads(line)["content"] == "日本語テスト"


class TestSessionReset:
    @pytest.mark.asyncio
    async def test_reset_clears_messages(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "hello"})
        session.reset()
        assert session.messages == []

    @pytest.mark.asyncio
    async def test_reset_generates_new_session_id(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        old_id = session.session_id
        session.reset()
        assert session.session_id != old_id

    @pytest.mark.asyncio
    async def test_reset_updates_sessions_json(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        session.reset()
        with open(tmp_path / "sessions.json") as f:
            sessions = json.load(f)
        assert sessions["test:key"]["sessionId"] == session.session_id

    @pytest.mark.asyncio
    async def test_reset_preserves_old_jsonl(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "old"})
        old_jsonl = session.session_jsonl_path
        session.reset()
        assert old_jsonl.exists()

    @pytest.mark.asyncio
    async def test_reset_points_to_new_jsonl(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        old_jsonl = session.session_jsonl_path
        session.reset()
        assert session.session_jsonl_path != old_jsonl

    @pytest.mark.asyncio
    async def test_after_reset_new_messages_in_new_jsonl(self, tmp_path):
        session = await Session.load(tmp_path, "test:key")
        await session.append_message({"role": "user", "content": "before reset"})
        session.reset()
        await session.append_message({"role": "user", "content": "after reset"})
        # 新しいJSONLには "after reset" だけあるべき
        lines = session.session_jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["content"] == "after reset"
