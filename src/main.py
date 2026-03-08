import asyncio
import argparse
import os

from .agent.agent import CowakaClawAgent
from .ui import CLI


async def async_main(cli_args):
    if cli_args.ui == "slack":
        from .ui import Slack
        ui = Slack(
            default_channel_id=cli_args.slack_channel,
        )
    else:
        ui = CLI()

    async with CowakaClawAgent(
        model=cli_args.model or os.getenv("COWAKA_CLAW_OPENAI_MODEL"),
        base_dir_path=cli_args.base_dir,
        workspace_path=cli_args.workspace,
        mcp_config_json_path=cli_args.mcp_config,
        ui=ui,
    ) as agent:
        await agent.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", choices=["cli", "slack"], default="cli")
    parser.add_argument("--slack-channel", help="Slack default channel ID (required for --ui slack)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-dir", default="./base_dir")
    parser.add_argument("--workspace", default="./workspace")
    parser.add_argument("--mcp-config", default="./mcp_config.json")

    cli_args = parser.parse_args()

    if cli_args.ui == "slack" and not cli_args.slack_channel:
        parser.error("--slack-channel is required when --ui slack")

    try:
        asyncio.run(async_main(cli_args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
