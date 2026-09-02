import argparse
import asyncio
import json
import logging
import signal
from dataclasses import replace
from datetime import datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

from .tools.agenda import AgendaTools
from .channel import login_channel
from .config import ConfigError, load_config
from .emotions import managed_emotion_path, remove_unreferenced_emotion_asset
from .logging_context import configure_logging, log_event
from .models import ToolCall, TurnDraft
from .runtime import MomoiDaemon
from .semantic import SemanticRecallService
from .storage import Store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the headless Momoi daemon")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('momoi')}")
    parser.add_argument(
        "--workspace",
        type=lambda value: Path(value).expanduser(),
        default=Path.home() / ".momoi",
        help="runtime workspace (default: ~/.momoi)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run the Momoi daemon")
    run_parser.add_argument(
        "--dashboard",
        action="store_true",
        help="serve the local Web dashboard (/api requires JWT from dashboard.token)",
    )
    run_parser.add_argument(
        "--dashboard-host",
        default="0.0.0.0",
        help="dashboard bind host (default: 0.0.0.0)",
    )
    run_parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8788,
        help="dashboard bind port (default: 8788)",
    )
    channel_parser = commands.add_parser("channel", help="manage the active channel")
    channel_commands = channel_parser.add_subparsers(
        dest="channel_command", required=True
    )
    login_parser = channel_commands.add_parser(
        "login", help="authenticate a configured channel"
    )
    login_parser.add_argument("channel_name", nargs="?")
    emotion_parser = commands.add_parser("emotion", help="manage emotion image assets")
    emotion_commands = emotion_parser.add_subparsers(
        dest="emotion_command", required=True
    )
    add_parser = emotion_commands.add_parser("add", help="import or update an asset")
    add_parser.add_argument("--slug", required=True)
    add_parser.add_argument("--path", required=True)
    add_parser.add_argument("--desc", required=True)
    del_parser = emotion_commands.add_parser("del", help="delete an asset")
    del_parser.add_argument("--slug", required=True)
    emotion_commands.add_parser("list", help="list assets")
    goal_parser = commands.add_parser("goal", help="manage persistent goals")
    goal_commands = goal_parser.add_subparsers(dest="goal_command", required=True)
    goal_add = goal_commands.add_parser("add", help="create a goal")
    goal_add.add_argument("--title", required=True)
    goal_add.add_argument("--success", required=True)
    goal_add.add_argument("--action", required=True)
    timing = goal_add.add_mutually_exclusive_group()
    timing.add_argument("--at", help="next review as an ISO 8601 timestamp")
    timing.add_argument("--every-seconds", type=int)
    timing.add_argument(
        "--daily",
        metavar="HH:MM",
        action="append",
        help="daily local time; repeat for multiple times",
    )
    goal_list = goal_commands.add_parser("list", help="list goals")
    goal_list.add_argument("--all", action="store_true", dest="include_closed")
    goal_del = goal_commands.add_parser("del", help="cancel a goal")
    goal_del.add_argument("goal_id", help="full ID or unambiguous prefix")
    goal_del.add_argument("--reason", default="Cancelled from CLI")
    embedding_parser = commands.add_parser(
        "embedding", help="manage the local semantic recall index"
    )
    embedding_commands = embedding_parser.add_subparsers(
        dest="embedding_command", required=True
    )
    embedding_commands.add_parser("status", help="show index and sidecar status")
    embedding_commands.add_parser(
        "reconcile", help="queue missing or stale semantic sources"
    )
    embedding_build = embedding_commands.add_parser(
        "build", help="create and populate a building embedding space"
    )
    embedding_build.add_argument("--wait", action="store_true")
    embedding_activate = embedding_commands.add_parser(
        "activate", help="atomically activate a completed building space"
    )
    embedding_activate.add_argument("space_id", nargs="?")
    return parser.parse_args()


def emotion(args: argparse.Namespace) -> None:
    config_path = args.workspace / "config.json"
    config = load_config(config_path)
    store = Store(
        config.database,
        args.workspace,
        thinking=config.thinking,
        timezone=config.timezone,
    )
    try:
        if args.emotion_command == "add":
            previous = store.emotion(args.slug)
            managed = managed_emotion_path(args.workspace, args.path)
            item = store.add_emotion(args.slug, managed, args.desc)
            if previous and previous["path"] != item["path"]:
                remove_unreferenced_emotion_asset(
                    store, str(previous["path"]), args.workspace
                )
            print(f"added\t{item['id']}\t{item['slug']}\t{item['path']}\t{item['description']}")
        elif args.emotion_command == "del":
            item = store.emotion(args.slug)
            if item is None:
                raise ValueError("emotion slug not found")
            referenced = store.emotion_path_referenced(
                str(item["path"]), exclude_slug=args.slug
            )
            if not store.delete_emotion(args.slug):
                raise ValueError("emotion slug not found")
            if not referenced:
                remove_unreferenced_emotion_asset(
                    store, str(item["path"]), args.workspace
                )
            print(f"deleted\t{args.slug}")
        else:
            for item in store.list_emotions():
                print(f"{item['id']}\t{item['slug']}\t{item['path']}\t{item['description']}")
    finally:
        store.close()


def goal(args: argparse.Namespace) -> None:
    config = load_config(args.workspace / "config.json")
    store = Store(
        config.database,
        args.workspace,
        thinking=config.thinking,
        timezone=config.timezone,
    )
    try:
        if args.goal_command == "list":
            for item in store.list_goals(args.include_closed):
                review = (
                    datetime.fromtimestamp(
                        float(item["next_review_at"]), store.timezone
                    )
                    .isoformat(timespec="seconds")
                    if item["next_review_at"] is not None
                    else "-"
                )
                print(
                    f"{item['id']}\t{item['status']}\t{review}\t"
                    f"{item['title']}\t{item['next_action']}"
                )
            return

        tools = AgendaTools(store)
        draft = TurnDraft()
        if args.goal_command == "add":
            arguments = {
                "title": args.title,
                "success_criteria": args.success,
                "next_action": args.action,
            }
            if args.every_seconds is not None:
                arguments["schedule"] = {
                    "kind": "interval",
                    "every_seconds": args.every_seconds,
                }
            elif args.daily is not None:
                arguments["schedule"] = {
                    "kind": "daily",
                    "times": args.daily,
                }
            else:
                arguments["next_review_at"] = args.at or (
                    datetime.now(store.timezone) + timedelta(seconds=1)
                ).isoformat()
            result = tools.execute(
                ToolCall("cli-goal-add", "goal_create", arguments),
                draft,
                authority="owner",
                source_event_id="cli:goal",
                allow_notify=False,
            )
        else:
            matches = [
                item
                for item in store.list_goals(include_closed=False)
                if str(item["id"]).startswith(args.goal_id)
            ]
            if len(matches) != 1:
                raise ValueError("goal id prefix not found or ambiguous")
            result = tools.execute(
                ToolCall(
                    "cli-goal-del",
                    "goal_cancel",
                    {"goal_id": matches[0]["id"], "reason": args.reason},
                ),
                draft,
                authority="owner",
                source_event_id="cli:goal",
                allow_notify=False,
            )
        if not result.get("ok"):
            raise ValueError(str(result.get("message") or result.get("error")))
        store.commit_goal_draft(draft)
        item = result["goal"]
        verb = "added" if args.goal_command == "add" else "deleted"
        print(f"{verb}\t{item['id']}\t{item['status']}\t{item['title']}")
    finally:
        store.close()


async def embedding(args: argparse.Namespace) -> None:
    config = load_config(args.workspace / "config.json")
    embedding_config = replace(config.embedding, enabled=True)
    store = Store(
        config.database,
        args.workspace,
        thinking=config.thinking,
        timezone=config.timezone,
    )
    service = SemanticRecallService(
        store,
        embedding_config,
        auto_activate=False,
        policy=config.policies.semantic,
    )
    try:
        if args.embedding_command == "status":
            status = store.semantic_status()
            healthy, latency_ms, error = await service.client.health()
            status["sidecar"] = {
                "healthy": healthy,
                "latency_ms": round(latency_ms, 2),
                "error": error,
            }
            print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
            return
        if args.embedding_command == "activate":
            space_id = args.space_id
            if not space_id:
                building = store.semantic_space(state="building")
                if building is None:
                    raise ValueError("building semantic space not found")
                space_id = str(building["id"])
            store.activate_semantic_space(space_id)
            print(f"activated\t{space_id}")
            return
        space = store.ensure_semantic_space(
            model=embedding_config.model,
            dimensions=embedding_config.dimensions,
            calibration_profile=embedding_config.calibration_profile,
        )
        queued = store.reconcile_semantic_sources(str(space["id"]))
        if args.embedding_command == "reconcile":
            print(f"reconciled\t{space['id']}\tqueued={queued}")
            return
        if not args.wait:
            print(f"building\t{space['id']}\tqueued={queued}")
            return
        while True:
            await service.maintain_once()
            status = store.semantic_status(str(space["id"]))
            if (
                status["eligible_source_coverage"] >= 1.0
                and not status["pending"]
                and not status["encoding"]
                and not status["retry"]
                and not status["dirty_sources"]
            ):
                print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
                return
            await asyncio.sleep(0.05)
    finally:
        await service.close()
        store.close()


async def run(
    config_path: str | Path,
    *,
    dashboard: bool = False,
    dashboard_host: str = "0.0.0.0",
    dashboard_port: int = 8788,
) -> None:
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    config = load_config(config_path)
    if dashboard and not config.dashboard.token:
        raise ValueError("dashboard.token is required when --dashboard is enabled")
    configure_logging(
        getattr(logging, config.log_level, logging.INFO),
        ZoneInfo(config.timezone),
    )
    for noisy_logger in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if sig := getattr(signal, name, None):
            loop.add_signal_handler(sig, stop.set)
    log_event(
        logging.getLogger(__name__),
        logging.INFO,
        "service_start",
        model=config.llm.model,
        channels=",".join(
            str(getattr(item, "plugin", "unknown"))
            for item in config.channel_configs
        ),
        primary_channel=getattr(config.channel, "plugin", "unknown"),
        soul_prompt_path=str(config.soul_prompt_path or ""),
        soul_prompt_chars=len(config.soul_prompt),
        heartbeat_prompt_path=str(config.heartbeat_prompt_path or ""),
        heartbeat_prompt_chars=len(config.heartbeat_prompt),
    )
    dashboard_bind = (dashboard_host, dashboard_port) if dashboard else None
    await MomoiDaemon(config, dashboard=dashboard_bind).run(stop)


async def channel(args: argparse.Namespace) -> None:
    config = load_config(args.workspace / "config.json")
    if args.channel_command == "login":
        if args.channel_name is None:
            if len(config.channel_configs) != 1:
                raise ValueError("channel name is required when multiple channels are configured")
            selected = config.channel_configs[0]
        else:
            selected = next(
                (
                    item
                    for item in config.channel_configs
                    if getattr(item, "plugin", "") == args.channel_name
                ),
                None,
            )
            if selected is None:
                raise ValueError(f"configured channel not found: {args.channel_name}")
        await login_channel(selected)


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


if __name__ == "__main__":
    main()
