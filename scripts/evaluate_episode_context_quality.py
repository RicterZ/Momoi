#!/usr/bin/env python3
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from momoi.config import load_config
from momoi.runtime.context_assembler import (
    assemble_main_context,
    build_plan_retrieval,
)
from momoi.storage import Store, estimate_tokens


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--config", type=Path, required=True)
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
        config = load_config(args.config)
        rows = []
        before_timestamp = None
        for record in active_plans(store, max(1, args.turns)):
            plan = record.get("plan")
            if not isinstance(plan, dict):
                continue
            retrieval = build_plan_retrieval(store, plan, config)
            before_timestamp = float(record["updated_at"])
            context = assemble_main_context(
                store,
                retrieval,
                config.summary_tokens,
                config.recent_raw_tokens,
                0,
                before_timestamp,
            )
            directory = context["episodes"]
            rows.append(
                {
                    "turn_id": record["turn_id"],
                    "units": len(plan.get("intent_units", [])),
                    "recall_queries": sum(
                        len(unit.get("recall_queries", []))
                        for unit in plan.get("intent_units", [])
                        if isinstance(unit, dict)
                    ),
                    "episodes": directory.count("[episode id="),
                    "tokens": estimate_tokens(directory) if directory else 0,
                    "raw_markers": directory.count("matched_raw:")
                    + directory.count("raw_tail:"),
                }
            )
        tokens = sorted(int(row["tokens"]) for row in rows)

        def percentile(fraction: float) -> int:
            if not tokens:
                return 0
            return tokens[min(len(tokens) - 1, int((len(tokens) - 1) * fraction))]

        print(
            json.dumps(
                {
                    "turns": len(rows),
                    "turns_with_episode_directory": sum(
                        int(row["episodes"]) > 0 for row in rows
                    ),
                    "turns_without_recall_but_with_episode_directory": sum(
                        int(row["recall_queries"]) == 0 and int(row["episodes"]) > 0
                        for row in rows
                    ),
                    "raw_markers": sum(int(row["raw_markers"]) for row in rows),
                    "episode_directory_tokens": {
                        "p50": percentile(0.50),
                        "p95": percentile(0.95),
                        "max": max(tokens, default=0),
                    },
                    "max_episodes": max(
                        (int(row["episodes"]) for row in rows), default=0
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        store.close()


if __name__ == "__main__":
    main()
