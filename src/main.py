import asyncio
import argparse
import os

import openai

from .agent.agent import CowakaClawAgent
from .ui import CLI


async def select_model_interactively() -> str:
    client = openai.AsyncOpenAI()
    response = await client.models.list()
    models = sorted(response.data, key=lambda m: m.id)
    if len(models) == 1:
        print(f"Model auto-selected (only one available): {models[0].id}")
        print("(Tip: set a default with --model <name> or COWAKA_CLAW_OPENAI_MODEL=<name>)")
        return models[0].id
    for i, model in enumerate(models):
        print(f"  [{i}] {model.id}")
    print("(Tip: set a default with --model <name> or COWAKA_CLAW_OPENAI_MODEL=<name>)")
    choice = (await asyncio.to_thread(input, "Select model (number or name): ")).strip()
    if choice.isdigit():
        idx = int(choice)
        if 0 <= idx < len(models):
            return models[idx].id
        raise ValueError(f"Index out of range: {idx}")
    ids = [m.id for m in models]
    if choice in ids:
        return choice
    raise ValueError(f"Unknown model: {choice!r}")


async def async_main(cli_args):
    if cli_args.ui == "slack":
        from .ui import Slack
        ui = Slack(
            default_channel_id=cli_args.slack_channel,
        )
    else:
        ui = CLI()

    model = cli_args.model or os.getenv("COWAKA_CLAW_OPENAI_MODEL")
    if not model:
        model = await select_model_interactively()

    async with CowakaClawAgent(
        model=model,
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
