#!/usr/bin/env python3
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from momoi.runtime.context_candidates import (
    EpisodeCandidatePolicy,
    collect_episode_candidates,
    full_candidate_context,
)
from momoi.storage import Store, estimate_tokens


def owner_query(store: Store, source_ids: list[str]) -> str:
    if not source_ids:
        return ""
    placeholders = ",".join("?" for _ in source_ids)
    rows = store._db.execute(
        f"""SELECT content FROM events
            WHERE id IN ({placeholders}) ORDER BY received_at""",
        source_ids,
    ).fetchall()
    return "\n".join(str(row["content"]) for row in rows)


def active_plans(store: Store, limit: int) -> list[dict[str, object]]:
    rows = store._db.execute(
        """SELECT cp.* FROM context_plans AS cp
           JOIN turns AS t ON t.id=cp.turn_id
           WHERE cp.state<>'superseded' AND t.kind='owner'
             AND t.state='completed'
           ORDER BY t.updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [store._context_plan_dict(row) for row in rows]


def evaluate_episode_policy(
    store: Store,
    plans: list[dict[str, object]],
    policy: EpisodeCandidatePolicy,
) -> dict[str, object]:
    expected = 0
    covered = 0
    tokens = []
    counts = []
    for record in plans:
        plan = record["plan"]
        if not isinstance(plan, dict):
            continue
        query = owner_query(store, list(record["source_event_ids"]))
        candidates = collect_episode_candidates(store, query, policy)
        candidate_ids = {str(item["id"]) for item in candidates}
        expected_ids = {
            str(item["episode_id"])
            for item in plan.get("episode_bindings", [])
            if isinstance(item, dict) and item.get("is_new") is False
        }
        expected += len(expected_ids)
        covered += len(expected_ids & candidate_ids)
        counts.append(len(candidates))
        tokens.append(
            estimate_tokens(
                json.dumps(
                    full_candidate_context(candidates),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        )
    return {
        "search": policy.search_limit,
        "active": policy.active_limit,
        "directory": policy.directory_limit,
        "total": policy.total_limit,
        "coverage": covered / expected if expected else 1.0,
        "covered": covered,
        "expected": expected,
        "avg_candidates": sum(counts) / len(counts) if counts else 0,
        "avg_tokens": sum(tokens) / len(tokens) if tokens else 0,
        "max_tokens": max(tokens, default=0),
    }


def evaluate_memory_limits(
    store: Store, plans: list[dict[str, object]]
) -> list[dict[str, object]]:
    results = []
    for limit in (0, 2, 4, 6, 8):
        expected = 0
        covered = 0
        for record in plans:
            retrieval = record.get("retrieval")
            if not isinstance(retrieval, dict):
                continue
            expected_ids = {
                int(item["id"])
                for item in retrieval.get("confirmed_memories", [])
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            }
            expected += len(expected_ids)
            if not limit:
                continue
            query = owner_query(store, list(record["source_event_ids"]))
            actual_ids = {
                int(item["id"])
                for item in store.search_memories(
                    query, limit, activation="recall"
                )
            }
            covered += len(expected_ids & actual_ids)
        results.append(
            {
                "limit": limit,
                "coverage": covered / expected if expected else 1.0,
                "covered": covered,
                "expected": expected,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--turns", type=int, default=500)
    args = parser.parse_args()
    source = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    with tempfile.TemporaryDirectory() as directory:
        copy_path = Path(directory) / "evaluation.sqlite3"
        destination = sqlite3.connect(copy_path)
        source.backup(destination)
        destination.close()
        source.close()
        store = Store(copy_path)
        plans = active_plans(store, max(1, args.turns))
        policies = []
        for search in (4, 6, 8):
            for active in (2, 4, 8, 12):
                for directory in (0, 8, 16, 24, 32, 64):
                    total = min(64, search + active + directory)
                    policies.append(
                        EpisodeCandidatePolicy(
                            search, active, directory, total
                        )
                    )
        episode_results = [
            evaluate_episode_policy(store, plans, policy) for policy in policies
        ]
        baseline = evaluate_episode_policy(
            store, plans, EpisodeCandidatePolicy()
        )
        viable = [
            item
            for item in episode_results
            if item["coverage"] >= baseline["coverage"] * 0.99
        ]
        viable.sort(key=lambda item: (item["avg_tokens"], -item["coverage"]))
        print(
            json.dumps(
                {
                    "plans": len(plans),
                    "baseline": baseline,
                    "best_episode_policies": viable[:10],
                    "memory_limits": evaluate_memory_limits(store, plans),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        store.close()


if __name__ == "__main__":
    main()
