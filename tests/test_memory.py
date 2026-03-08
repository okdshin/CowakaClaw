import pytest

from src.memory.memory import call_memory_update


class TestCallMemoryUpdate:
    @pytest.fixture
    def workspace(self, tmp_path):
        return tmp_path

    def test_creates_new_file(self, workspace):
        call_memory_update(workspace, "Notes", "some content")
        assert (workspace / "MEMORY.md").exists()

    def test_new_section_has_heading(self, workspace):
        call_memory_update(workspace, "Test Section", "some content")
        text = (workspace / "MEMORY.md").read_text()
        assert "## Test Section" in text

    def test_new_section_has_content(self, workspace):
        call_memory_update(workspace, "Notes", "my note")
        text = (workspace / "MEMORY.md").read_text()
        assert "my note" in text

    def test_new_section_has_timestamp(self, workspace):
        call_memory_update(workspace, "Notes", "content")
        text = (workspace / "MEMORY.md").read_text()
        assert "<!-- updated:" in text

    def test_append_adds_to_existing_section(self, workspace):
        call_memory_update(workspace, "Notes", "first")
        call_memory_update(workspace, "Notes", "second")
        text = (workspace / "MEMORY.md").read_text()
        assert "first" in text
        assert "second" in text

    def test_append_preserves_order(self, workspace):
        call_memory_update(workspace, "Notes", "first entry")
        call_memory_update(workspace, "Notes", "second entry")
        text = (workspace / "MEMORY.md").read_text()
        assert text.index("first entry") < text.index("second entry")

    def test_replace_overwrites_section_content(self, workspace):
        call_memory_update(workspace, "Notes", "old content")
        call_memory_update(workspace, "Notes", "new content", mode="replace")
        text = (workspace / "MEMORY.md").read_text()
        assert "old content" not in text
        assert "new content" in text

    def test_replace_keeps_heading(self, workspace):
        call_memory_update(workspace, "Notes", "content")
        call_memory_update(workspace, "Notes", "replaced", mode="replace")
        text = (workspace / "MEMORY.md").read_text()
        assert "## Notes" in text

    def test_multiple_sections_all_present(self, workspace):
        call_memory_update(workspace, "Section A", "content A")
        call_memory_update(workspace, "Section B", "content B")
        text = (workspace / "MEMORY.md").read_text()
        assert "## Section A" in text
        assert "## Section B" in text
        assert "content A" in text
        assert "content B" in text

    def test_sections_added_in_order(self, workspace):
        call_memory_update(workspace, "Alpha", "a")
        call_memory_update(workspace, "Beta", "b")
        text = (workspace / "MEMORY.md").read_text()
        assert text.index("## Alpha") < text.index("## Beta")

    def test_updating_one_section_does_not_corrupt_others(self, workspace):
        call_memory_update(workspace, "Alpha", "alpha content")
        call_memory_update(workspace, "Beta", "beta content")
        call_memory_update(workspace, "Alpha", "alpha updated", mode="replace")
        text = (workspace / "MEMORY.md").read_text()
        assert "beta content" in text
        assert "alpha updated" in text
        assert "alpha content" not in text

    def test_returns_success_message(self, workspace):
        result = call_memory_update(workspace, "Section", "content")
        assert "MEMORY.md updated" in result

    def test_returns_section_name_in_result(self, workspace):
        result = call_memory_update(workspace, "My Section", "content")
        assert "My Section" in result

    def test_returns_mode_in_result(self, workspace):
        result = call_memory_update(workspace, "Sec", "content", mode="replace")
        assert "replace" in result

    def test_append_mode_multiple_timestamps(self, workspace):
        call_memory_update(workspace, "Log", "entry 1")
        call_memory_update(workspace, "Log", "entry 2")
        text = (workspace / "MEMORY.md").read_text()
        assert text.count("<!-- updated:") == 2

    def test_replace_mode_single_timestamp(self, workspace):
        call_memory_update(workspace, "Log", "entry 1")
        call_memory_update(workspace, "Log", "entry 2", mode="replace")
        text = (workspace / "MEMORY.md").read_text()
        assert text.count("<!-- updated:") == 1

    def test_empty_file_handled(self, workspace):
        """空ファイルが存在する場合もクラッシュしない"""
        (workspace / "MEMORY.md").write_text("")
        call_memory_update(workspace, "Notes", "content")
        text = (workspace / "MEMORY.md").read_text()
        assert "## Notes" in text
        assert "content" in text
