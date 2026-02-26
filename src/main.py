import asyncio

from .agent.agent import CowakaClawAgent


async def main():
    async with CowakaClawAgent(
        model="openai-gpt-oss-20b",
        base_dir_path="./base_dir",
        workspace_path="./workspace",
        mcp_config_json_path="./mcp_config.json",
    ) as agent:
        await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
