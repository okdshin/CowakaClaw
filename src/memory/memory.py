import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# MEMORY.md への read-modify-write をアトミックにするロック。
# call_memory_update は asyncio.to_thread 経由で複数スレッドから同時に呼ばれるため。
memory_md_lock = threading.Lock()


class MemoryUpdate(BaseModel):
    """
    Update MEMORY.md to persist important information across sessions.
    Call this when:
    - User shares personal facts, preferences, or goals
    - A decision or conclusion is reached
    - An ongoing project or task is established
    - User corrects previously stored information (use mode='replace')
    Do NOT call for temporary or session-specific information.
    """
    section: str = Field(..., description="Markdown heading to update (e.g. 'User Preferences')")
    content: str = Field(..., description="Content to write under the section")
    mode: Literal["append", "replace"] = Field("append", description="'append' or 'replace'")


def call_memory_update(
    workspace_path: Path,
    section: str,
    content: str,
    mode: str = "append",
) -> str:
    memory_path = workspace_path / "MEMORY.md"
    with memory_md_lock:
        if memory_path.exists():
            with open(memory_path) as f:
                current = f.read()
        else:
            current = ""

        heading = f"## {section}"
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        new_entry = f"{content}\n<!-- updated: {timestamp} -->"

        if heading in current:
            # セクションを見つけて処理
            before, _, rest = current.partition(heading)
            # 次のセクション（##）までを切り出す
            parts = rest.split("\n## ", 1)
            section_body = parts[0]
            after = ("\n## " + parts[1]) if len(parts) > 1 else ""

            if mode == "replace":
                new_section = f"{heading}\n{new_entry}"
            else:  # append
                new_section = f"{heading}{section_body}\n{new_entry}"

            updated = before + new_section + after
        else:
            # セクションが存在しない → 末尾に追加
            updated = current.rstrip() + f"\n\n{heading}\n{new_entry}\n"

        memory_path.write_text(updated, encoding="utf-8")
    return f"MEMORY.md updated: section='{section}', mode={mode}"
