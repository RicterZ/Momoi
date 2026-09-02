import argparse
from importlib.metadata import version
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the headless Momoi daemon")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('momoi')}"
    )
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
