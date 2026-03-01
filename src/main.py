import asyncio
import os

from .agent.agent import CowakaClawAgent
from .ui import CLI


async def async_main():
    async with CowakaClawAgent(
        model=os.getenv("COWAKA_CLAW_OPENAI_MODEL"),
        base_dir_path="./base_dir",
        workspace_path="./workspace",
        mcp_config_json_path="./mcp_config.json",
        ui=CLI(),
    ) as agent:
        await agent.run()


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
