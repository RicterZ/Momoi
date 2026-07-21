import argparse
import asyncio
import hashlib
import logging
import re
import shutil
import signal
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .agenda_tools import AgendaTools
from .config import ConfigError, load_config
from .daemon import MomoiDaemon
from .models import ToolCall, TurnDraft
from .store import Store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the headless Momoi daemon")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--workspace",
        type=lambda value: Path(value).expanduser(),
        default=Path.home() / ".momoi",
        help="runtime workspace (default: ~/.momoi)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the Momoi daemon")
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
    timing.add_argument("--daily", metavar="HH:MM")
    goal_list = goal_commands.add_parser("list", help="list goals")
    goal_list.add_argument("--all", action="store_true", dest="include_closed")
    goal_del = goal_commands.add_parser("del", help="cancel a goal")
    goal_del.add_argument("goal_id", help="full ID or unambiguous prefix")
    goal_del.add_argument("--reason", default="Cancelled from CLI")
    return parser.parse_args()


def _managed_emotion_path(config_path: str | Path, source_value: str) -> Path:
    source = Path(source_value).expanduser().resolve()
    if not source.is_file():
        raise ValueError("path must be an existing file")
    extension = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ValueError("emotion file needs a simple extension")
    digest = hashlib.md5(usedforsecurity=False)
    with source.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    directory = Path(config_path).expanduser().resolve().parent / "emotion"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest.hexdigest()}{extension}"
    if not destination.exists():
        shutil.copy2(source, destination)
    return destination


def _remove_unreferenced_asset(
    store: Store, path: str, config_path: str | Path
) -> None:
    asset = Path(path)
    directory = Path(config_path).expanduser().resolve().parent / "emotion"
    if asset.is_relative_to(directory) and not store.emotion_path_referenced(str(asset)):
        asset.unlink(missing_ok=True)


def emotion(args: argparse.Namespace) -> None:
    config_path = args.workspace / "config.json"
    config = load_config(config_path)
    store = Store(config.database, args.workspace)
    try:
        if args.emotion_command == "add":
            previous = store.emotion(args.slug)
            managed = _managed_emotion_path(config_path, args.path)
            item = store.add_emotion(args.slug, managed, args.desc)
            if previous and previous["path"] != item["path"]:
                _remove_unreferenced_asset(store, str(previous["path"]), config_path)
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
                _remove_unreferenced_asset(store, str(item["path"]), config_path)
            print(f"deleted\t{args.slug}")
        else:
            for item in store.list_emotions():
                print(f"{item['id']}\t{item['slug']}\t{item['path']}\t{item['description']}")
    finally:
        store.close()


def goal(args: argparse.Namespace) -> None:
    config = load_config(args.workspace / "config.json")
    store = Store(config.database, args.workspace)
    try:
        if args.goal_command == "list":
            for item in store.list_goals(args.include_closed):
                review = (
                    datetime.fromtimestamp(float(item["next_review_at"]))
                    .astimezone()
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
                    "timezone": config.notifications.timezone,
                    "every_seconds": args.every_seconds,
                }
            elif args.daily is not None:
                arguments["schedule"] = {
                    "kind": "daily",
                    "timezone": config.notifications.timezone,
                    "at": args.daily,
                }
            else:
                arguments["next_review_at"] = args.at or (
                    datetime.now().astimezone() + timedelta(seconds=1)
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


async def run(config_path: str | Path) -> None:
    config = load_config(config_path)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy_logger in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if sig := getattr(signal, name, None):
            loop.add_signal_handler(sig, stop.set)
    logging.getLogger(__name__).info(
        "Starting Momoi model=%s", config.llm.model
    )
    await MomoiDaemon(config).run(stop)


def main() -> None:
    args = parse_args()
    try:
        if args.command == "run":
            asyncio.run(run(args.workspace / "config.json"))
        elif args.command == "emotion":
            emotion(args)
        elif args.command == "goal":
            goal(args)
    except (ConfigError, ValueError, OSError) as error:
        raise SystemExit(f"configuration error: {error}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
