#!/usr/bin/env python3
"""Compare sparse and hybrid recall with private, externally stored labels.

The labels file must remain outside the repository. Query text is read from the
database's stored Context Plan; this script never prints it or source content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from momoi.config import EmbeddingConfig
from momoi.runtime.context_assembler import select_plan_recall_queries
from momoi.semantic import SemanticRecallService
from momoi.storage import MemoryRecallQuery, Store
from momoi.storage.episode_ranking import EpisodeRecallQuery


@dataclass(frozen=True)
class Expected:
    pool: str
    source_id: str
    max_rank: int

    @property
    def identity(self) -> str:
        return f"{self.pool}:{self.source_id}"


@dataclass(frozen=True)
class Case:
    name: str
    turn_id: str
    expected: tuple[Expected, ...]
    forbidden: frozenset[str]
    expected_empty: frozenset[str]


def load_cases(path: Path) -> tuple[Case, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("label file must contain a JSON array")
    return tuple(
        Case(
            name=str(item["name"]),
            turn_id=str(item["turn_id"]),
            expected=tuple(
                Expected(
                    pool=str(expected["pool"]),
                    source_id=str(expected["source_id"]),
                    max_rank=int(expected["max_rank"]),
                )
                for expected in item.get("expected", [])
            ),
            forbidden=frozenset(str(value) for value in item.get("forbidden", [])),
            expected_empty=frozenset(
                str(value) for value in item.get("expected_empty", [])
            ),
        )
        for item in raw
    )


def recall_queries(
    plan: dict[str, object],
) -> tuple[list[MemoryRecallQuery], list[EpisodeRecallQuery]]:
    selected, _reused, _emitted, _skipped = select_plan_recall_queries(plan)
    memory = [
        MemoryRecallQuery(
            expression=str(item["expression"]),
            unit_ids=tuple(str(value) for value in item["unit_ids"]),
            priority=int(item["priority"]),
        )
        for item in selected
    ]
    episodes = [
        EpisodeRecallQuery(query.expression, query.unit_ids, query.priority)
        for query in memory
    ]
    return memory, episodes


def identities(
    memories: list[dict[str, object]], episodes: list[dict[str, object]]
) -> dict[str, list[str]]:
    return {
        "confirmed": [
            f"confirmed:{row['id']}"
            for row in memories
            if row.get("source") == "confirmed"
        ],
        "reflection": [
            f"reflection:{row['id']}"
            for row in memories
            if row.get("source") == "reflection"
        ],
        "episode": [f"episode:{row['id']}" for row in episodes],
    }


async def run(args: argparse.Namespace) -> int:
    database = args.database.expanduser().resolve()
    labels = load_cases(args.labels.expanduser().resolve())
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    store = Store(database)
    config = EmbeddingConfig(
        enabled=True,
        endpoint=args.endpoint,
        model=args.model,
        dimensions=args.dimensions,
        calibration_profile=args.profile,
    )
    service = SemanticRecallService(store, config, auto_activate=False)
    service.start()
    if service.degraded_reason:
        raise RuntimeError(f"dense recall unavailable: {service.degraded_reason}")
    aggregate = {
        mode: {
            "checks": 0,
            "passed": 0,
            "reciprocal_rank": 0.0,
            "forbidden": 0,
            "returned": 0,
            "empty_checks": 0,
            "empty_passed": 0,
        }
        for mode in ("sparse", "hybrid")
    }
    dense_judged_positive = 0
    dense_judged_total = 0
    try:
        for case in labels:
            row = connection.execute(
                """SELECT plan_json FROM context_plans
                   WHERE turn_id=? ORDER BY revision DESC LIMIT 1""",
                (case.turn_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"benchmark Turn is missing: {case.name}")
            plan = json.loads(str(row["plan_json"]))
            memory_queries, episode_queries = recall_queries(plan)
            evidence = await service.prepare(
                memory_queries,
                output_limit=max(args.memory_top, args.episode_top),
            )
            runs = {
                "sparse": (
                    store.rank_recalled_memories(memory_queries, args.memory_top),
                    store.search_episode_queries(episode_queries, args.episode_top),
                ),
                "hybrid": (
                    store.rank_recalled_memories(
                        memory_queries,
                        args.memory_top,
                        dense_evidence=evidence,
                    ),
                    store.search_episode_queries(
                        episode_queries,
                        args.episode_top,
                        dense_evidence=evidence,
                    ),
                ),
            }
            case_results: dict[str, list[str]] = {}
            expected_identities = {expected.identity for expected in case.expected}
            for mode, (memories, episodes) in runs.items():
                pools = identities(memories, episodes)
                flattened = [
                    identity for values in pools.values() for identity in values
                ]
                case_results[mode] = flattened
                metrics = aggregate[mode]
                metrics["returned"] += len(flattened)
                metrics["forbidden"] += len(case.forbidden.intersection(flattened))
                for pool in case.expected_empty:
                    metrics["empty_checks"] += 1
                    if not pools.get(pool, []):
                        metrics["empty_passed"] += 1
                for expected in case.expected:
                    metrics["checks"] += 1
                    ranked_pool = pools.get(expected.pool, [])
                    rank = (
                        ranked_pool.index(expected.identity) + 1
                        if expected.identity in ranked_pool
                        else None
                    )
                    if rank is not None and rank <= expected.max_rank:
                        metrics["passed"] += 1
                        metrics["reciprocal_rank"] += 1.0 / rank
            for row in runs["hybrid"][0]:
                if not row.get("dense_only"):
                    continue
                identity = f"{row['source']}:{row['id']}"
                if identity in expected_identities:
                    dense_judged_positive += 1
                    dense_judged_total += 1
                elif identity in case.forbidden:
                    dense_judged_total += 1
            for row in runs["hybrid"][1]:
                if not row.get("dense_only"):
                    continue
                identity = f"episode:{row['id']}"
                if identity in expected_identities:
                    dense_judged_positive += 1
                    dense_judged_total += 1
                elif identity in case.forbidden:
                    dense_judged_total += 1
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "sparse_count": len(case_results["sparse"]),
                        "hybrid_count": len(case_results["hybrid"]),
                        "new_hybrid_results": len(
                            set(case_results["hybrid"]) - set(case_results["sparse"])
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        output: dict[str, object] = {"cases": len(labels)}
        for mode, metrics in aggregate.items():
            checks = int(metrics["checks"])
            returned = int(metrics["returned"])
            output[mode] = {
                **metrics,
                "recall": metrics["passed"] / checks if checks else 1.0,
                "mrr": metrics["reciprocal_rank"] / checks if checks else 1.0,
                "forbidden_rate": metrics["forbidden"] / returned if returned else 0.0,
            }
        output["dense_only_judged_precision"] = (
            dense_judged_positive / dense_judged_total if dense_judged_total else None
        )
        output["dense_only_judged_count"] = dense_judged_total
        print(json.dumps(output, ensure_ascii=False, indent=2))
        sparse = output["sparse"]
        hybrid = output["hybrid"]
        assert isinstance(sparse, dict) and isinstance(hybrid, dict)
        successful = (
            float(hybrid["recall"]) >= float(sparse["recall"])
            and float(hybrid["mrr"]) + args.max_mrr_drop >= float(sparse["mrr"])
            and float(hybrid["forbidden_rate"])
            <= float(sparse["forbidden_rate"]) + args.max_forbidden_increase
        )
        return 0 if successful else 1
    finally:
        await service.close()
        store.close()
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8002/v1/embeddings")
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--dimensions", type=int, default=512)
    parser.add_argument("--profile", default="bge-small-zh-v1.5-momoi-v1")
    parser.add_argument("--memory-top", type=int, default=6)
    parser.add_argument("--episode-top", type=int, default=8)
    parser.add_argument("--max-mrr-drop", type=float, default=0.02)
    parser.add_argument("--max-forbidden-increase", type=float, default=0.02)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
