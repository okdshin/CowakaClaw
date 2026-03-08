from pathlib import Path


def build_agent_system_prompt(workspace_path: Path) -> str:
    # Tooling
    # Safety
    # Skills
    # Self-update
    # Workspace
    # Documentation
    # Sandbox
    # Project context
    bootstrap_prompts = []
    for md_filename in ["SOUL", "MEMORY"]:
        path = workspace_path / f"{md_filename}.md"
        if path.exists():
            bootstrap_prompts.append(path.read_text().strip())
        else:
            print(f"[prompts] {md_filename}.md not found, skipping")
    return "\n\n".join(bootstrap_prompts)
