import argparse
import asyncio
import json
from datetime import datetime, timedelta

from ..channel import login_channel
from ..config.loading import load_config
from ..emotions import managed_emotion_path, remove_unreferenced_emotion_asset
from ..models import ToolCall, TurnDraft
from ..semantic.service import SemanticRecallService
from ..storage import Store
from ..tools.agenda import AgendaTools


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
            print(
                f"added\t{item['id']}\t{item['slug']}\t{item['path']}\t"
                f"{item['description']}"
            )
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
                print(
                    f"{item['id']}\t{item['slug']}\t{item['path']}\t"
                    f"{item['description']}"
                )
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
                    ).isoformat(timespec="seconds")
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
                arguments["next_review_at"] = (
                    args.at
                    or (datetime.now(store.timezone) + timedelta(seconds=1)).isoformat()
                )
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
    from ..integrations.registry import ServiceRegistry

    if not config.providers.enabled("embedding"):
        raise ValueError(
            "Enable the embedding binding in providers.yaml before using embedding commands"
        )
    services = ServiceRegistry(
        config.providers, semantic_policy=config.policies.semantic
    )
    embedding_config = services.embedding_config
    client = services.embedding
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
        client=client,
        policy=config.policies.semantic,
    )
    try:
        async with services:
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
        store.close()


async def channel(args: argparse.Namespace) -> None:
    config = load_config(args.workspace / "config.json")
    if args.channel_command == "login":
        if args.channel_name is None:
            if len(config.channel_configs) != 1:
                raise ValueError(
                    "channel name is required when multiple channels are configured"
                )
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
