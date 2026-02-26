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
        with open(workspace_path / f"{md_filename}.md") as mdf:
            md = mdf.read().strip()
            bootstrap_prompts.append(md)
    return "\n\n".join(bootstrap_prompts)
