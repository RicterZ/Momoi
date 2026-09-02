import asyncio

from ..config.models import ConfigError
from .commands import channel, embedding, emotion, goal
from .parser import parse_args
from .service import run


def main() -> None:
    args = parse_args()
    try:
        if args.command == "run":
            asyncio.run(
                run(
                    args.workspace / "config.json",
                    dashboard=args.dashboard,
                    dashboard_host=args.dashboard_host,
                    dashboard_port=args.dashboard_port,
                )
            )
        elif args.command == "channel":
            asyncio.run(channel(args))
        elif args.command == "emotion":
            emotion(args)
        elif args.command == "goal":
            goal(args)
        elif args.command == "embedding":
            asyncio.run(embedding(args))
    except (ConfigError, ValueError, OSError) as error:
        raise SystemExit(f"configuration error: {error}") from None
    except KeyboardInterrupt:
        pass
