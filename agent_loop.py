import openai
from pathlib import Path


class MCPClient:



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


class CowakaClawAgent:
    def __init__(self, model, workspace_path: str):
        self.model = model
        self.workspace_path = Path(workspace_path)
        self.openai_client = openai.OpenAI()

    def agent_loop(self):
        messages = []
        while True:
            agent_system_prompt = build_agent_system_prompt(workspace_path=self.workspace_path)
            user_message = input("> ")
            messages.append({"role": "user", "content": user_message})
            response = self.openai_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": agent_system_prompt}] + messages,
            )
            messages.append(
                {"role": "assistant", "content": response.choices[0].message.content}
            )
            print(response.choices[0].message.content)


def main():
    agent = CowakaClawAgent(model="openai-gpt-oss-20b", workspace_path="./workspace")
    agent.agent_loop()


if __name__ == "__main__":
    main()
