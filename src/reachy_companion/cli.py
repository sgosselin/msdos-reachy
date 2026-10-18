"""Local operator commands. No SDK, pairing or hardware imports."""

import argparse
import asyncio
import json

from .config import Config
from .ipc import Client
from .protocol import Fault


async def execute(config, args):
    if args.command == "new-chat":
        async with Client(config.worker_socket, config.worker_uid) as worker:
            await worker.call("runtime.stop", timeout=10)
        async with Client(config.adapter_socket, config.adapter_uid) as adapter:
            return await adapter.call("chat.new")
    async with Client(config.worker_socket, config.worker_uid) as client:
        if args.command == "status":
            return await client.call("runtime.status", timeout=4)
        if args.command == "tools":
            return await client.call("catalog.get")
        if args.command == "stop":
            return await client.call("runtime.stop", timeout=10)
        if args.command == "invoke":
            catalog = await client.call("catalog.get")
            try:
                params = json.loads(args.params)
            except ValueError:
                raise Fault("invalid_params", "parameters_json") from None
            return await client.call("tool.invoke", {"name": args.name, "params": params,
                                     "catalog_hash": catalog["hash"]}, timeout=125)
        if args.command == "say":
            return await client.call("conversation.run", {"text": args.text, "voice": args.voice}, timeout=175)
        if args.command == "ptt":
            # Explicit operator start, bounded capture duration. No always-on listener.
            return await client.call("conversation.run", {"seconds": args.seconds, "voice": True}, timeout=175)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stop", "new-chat"):
        commands.add_parser(name)
    tools = commands.add_parser("tools")
    tools.add_argument("action", choices=("list",))
    invoke = commands.add_parser("invoke")
    invoke.add_argument("name")
    invoke.add_argument("--params", default="{}")
    say = commands.add_parser("say")
    say.add_argument("text")
    say.add_argument("--voice", action="store_true")
    ptt = commands.add_parser("ptt")
    ptt.add_argument("--seconds", type=float, default=5, help="Capture 0.1–20 seconds after explicit invocation")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(Config.load(args.config), args))
        print(json.dumps(result, indent=2))
    except Fault as exc:
        parser.exit(1, json.dumps({"error": exc.as_dict()}) + "\n")
    except KeyboardInterrupt:
        parser.exit(130, "Local request interrupted.\n")


if __name__ == "__main__":
    main()
