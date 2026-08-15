#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from momoi.config import WebhookConfig, load_config
from momoi.logging_context import configure_logging
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_candidates import EpisodeCandidatePolicy


def active_plans(daemon: MomoiDaemon, limit: int) -> list[dict[str, object]]:
    rows = daemon.store._db.execute(
        """SELECT cp.* FROM context_plans AS cp
           JOIN turns AS t ON t.id=cp.turn_id
           WHERE cp.state<>'superseded' AND t.kind='owner'
             AND t.state='completed'
           ORDER BY t.updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [daemon.store._context_plan_dict(row) for row in rows]


def stratified_sample(
    plans: list[dict[str, object]], count: int
) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in plans:
        plan = record.get("plan")
        units = plan.get("intent_units", []) if isinstance(plan, dict) else []
        acts = sorted(
            {
                str(unit.get("speech_act") or "unknown")
                for unit in units
                if isinstance(unit, dict)
            }
        )
        groups["+".join(acts) or "unknown"].append(record)
    result = []
    while len(result) < count and any(groups.values()):
        for key in sorted(groups):
            if groups[key] and len(result) < count:
                result.append(groups[key].pop(0))
    return result


def event_speech_acts(plan: dict[str, object]) -> dict[str, str]:
    return {
        str(event_id): str(unit.get("speech_act") or "unknown")
        for unit in plan.get("intent_units", [])
        if isinstance(unit, dict)
        for event_id in unit.get("event_ids", [])
    }


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.workspace / "config.json")
    config = replace(
        config,
        database=args.database.resolve(),
        llm=replace(config.llm, dump_prompts=False),
        webhooks=WebhookConfig(),
    )
    daemon = MomoiDaemon(config)
    records = stratified_sample(
        active_plans(daemon, args.history), args.samples
    )
    policy = EpisodeCandidatePolicy(
        args.search, args.active, args.directory, args.total
    )
    results = []
    async with daemon.provider:
        for index, record in enumerate(records):
            source_ids = list(record["source_event_ids"])
            placeholders = ",".join("?" for _ in source_ids)
            rows = daemon.store._db.execute(
                f"""SELECT * FROM events WHERE id IN ({placeholders})
                    ORDER BY received_at""",
                source_ids,
            ).fetchall()
            events = [daemon.store._incoming_message(row) for row in rows]
            if len(events) != len(source_ids):
                continue
            target = record["plan"]
            if not isinstance(target, dict):
                continue
            turn_id = f"shadow:{uuid.uuid4().hex}"
            daemon.store.begin_turn(
                turn_id, "owner", [event.event_id for event in events]
            )
            started = time.monotonic()
            try:
                shadow = await daemon._plan_owner_context(
                    events, turn_id, candidate_policy=policy
                )
                error = ""
            except Exception as exception:
                shadow = {}
                error = type(exception).__name__
            usage = daemon.store.turn_usage(turn_id)
            target_acts = event_speech_acts(target)
            shadow_acts = event_speech_acts(shadow)
            target_episode_ids = {
                str(item["episode_id"])
                for item in target.get("episode_bindings", [])
                if isinstance(item, dict) and item.get("is_new") is False
            }
            shadow_episode_ids = {
                str(item["episode_id"])
                for item in shadow.get("episode_bindings", [])
                if isinstance(item, dict) and item.get("is_new") is False
            }
            results.append(
                {
                    "index": index,
                    "error": error,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "input_tokens": usage["input"],
                    "output_tokens": usage["output"],
                    "speech_act_equal": target_acts == shadow_acts,
                    "episode_recall": (
                        len(target_episode_ids & shadow_episode_ids)
                        / len(target_episode_ids)
                        if target_episode_ids
                        else 1.0
                    ),
                    "target_existing_episodes": len(target_episode_ids),
                    "shadow_existing_episodes": len(shadow_episode_ids),
                }
            )
    daemon.store.close()
    successful = [item for item in results if not item["error"]]
    print(
        json.dumps(
            {
                "policy": {
                    "search": args.search,
                    "active": args.active,
                    "directory": args.directory,
                    "total": args.total,
                },
                "requested_samples": args.samples,
                "completed_samples": len(results),
                "successful_samples": len(successful),
                "speech_act_agreement": (
                    sum(bool(item["speech_act_equal"]) for item in successful)
                    / len(successful)
                    if successful
                    else 0
                ),
                "episode_recall": (
                    sum(float(item["episode_recall"]) for item in successful)
                    / len(successful)
                    if successful
                    else 0
                ),
                "avg_input_tokens": (
                    sum(int(item["input_tokens"]) for item in successful)
                    / len(successful)
                    if successful
                    else 0
                ),
                "avg_output_tokens": (
                    sum(int(item["output_tokens"]) for item in successful)
                    / len(successful)
                    if successful
                    else 0
                ),
                "avg_duration_ms": (
                    sum(int(item["duration_ms"]) for item in successful)
                    / len(successful)
                    if successful
                    else 0
                ),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--history", type=int, default=200)
    parser.add_argument("--search", type=int, default=8)
    parser.add_argument("--active", type=int, default=2)
    parser.add_argument("--directory", type=int, default=8)
    parser.add_argument("--total", type=int, default=18)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    configure_logging(
        getattr(logging, str(args.log_level).upper(), logging.INFO)
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
