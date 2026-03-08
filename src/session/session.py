import asyncio
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

# sessions.json への read-modify-write をアトミックにするロック。
# _update_sessions_json は asyncio.to_thread 経由で複数スレッドから同時に呼ばれるため
# threading.Lock が必要。ロックはパスごとに分けることで、異なるディレクトリの
# Session インスタンスが互いをブロックしないようにする。
_sessions_json_locks: dict[str, threading.Lock] = {}
_sessions_json_locks_mutex = threading.Lock()


def _get_sessions_json_lock(sessions_json_path: "Path") -> threading.Lock:
    key = str(sessions_json_path)
    with _sessions_json_locks_mutex:
        if key not in _sessions_json_locks:
            _sessions_json_locks[key] = threading.Lock()
        return _sessions_json_locks[key]


def _new_session_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


class Session:
    def __init__(
        self,
        session_id: str,
        session_key: str,
        messages: list[dict],
        session_jsonl_path: Path,
        sessions_json_path: Path,
    ):
        self.session_id = session_id
        self.session_key = session_key
        self.messages = messages
        self.session_jsonl_path = session_jsonl_path
        self.sessions_json_path = sessions_json_path

    @classmethod
    def load(cls, sessions_dir: Path, session_key: str) -> "Session":
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_json_path = sessions_dir / "sessions.json"

        with _get_sessions_json_lock(sessions_json_path):
            # sessions.json を読む（なければ空dict）
            if sessions_json_path.exists():
                with open(sessions_json_path) as f:
                    sessions = json.load(f)
            else:
                sessions = {}

            # session_key に対応する session_id を解決（なければ新規発行）
            entry = sessions.get(session_key)
            if entry is not None:
                session_id = entry["sessionId"]
            else:
                session_id = _new_session_id()
                sessions[session_key] = {
                    "sessionId": session_id,
                    "updatedAt": datetime.now().astimezone().isoformat(),
                }
                with open(sessions_json_path, "w") as f:
                    json.dump(sessions, f, indent=2)

        # JSONL を読んで messages を復元（なければ空リスト）
        session_jsonl_path = sessions_dir / f"{session_id}.jsonl"
        messages = []
        if session_jsonl_path.exists():
            with open(session_jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        messages.append(json.loads(line))

        return cls(
            session_id=session_id,
            session_key=session_key,
            messages=messages,
            session_jsonl_path=session_jsonl_path,
            sessions_json_path=sessions_json_path,
        )

    async def append_message(self, message: dict) -> None:
        self.messages.append(message)
        await asyncio.to_thread(self._write_message, message)

    def _write_message(self, message: dict) -> None:
        with open(self.session_jsonl_path, "a") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._update_sessions_json()

    def reset(self) -> None:
        self.session_id = _new_session_id()
        self.session_jsonl_path = (
            self.session_jsonl_path.parent / f"{self.session_id}.jsonl"
        )
        self.messages = []
        self._update_sessions_json()
        # keep old session jsonl files

    def _update_sessions_json(self) -> None:
        with _get_sessions_json_lock(self.sessions_json_path):
            if self.sessions_json_path.exists():
                with open(self.sessions_json_path) as f:
                    sessions = json.load(f)
            else:
                sessions = {}
            sessions[self.session_key] = {
                "sessionId": self.session_id,
                "updatedAt": datetime.now().isoformat(),
            }
            with open(self.sessions_json_path, "w") as f:
                json.dump(sessions, f, indent=2)
