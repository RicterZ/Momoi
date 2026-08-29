#!/usr/bin/env python3
"""Run an externally labelled Episode-recall benchmark.

Keep the label file outside the repository: production Turn and Episode IDs,
query plans, and conversation topics are private.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from momoi.storage.episode_ranking import EpisodeRecallQuery
from momoi.storage.store import Store


@dataclass(frozen=True)
class ExpectedEpisode:
    episode_id: str
    max_rank: int


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    turn_id: str
    expected: tuple[ExpectedEpisode, ...] = ()
    expected_empty: bool = False


def _load_cases(path: Path) -> tuple[BenchmarkCase, ...]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("label file must contain a JSON array")
    return tuple(
        BenchmarkCase(
            name=str(item["name"]),
            turn_id=str(item["turn_id"]),
            expected=tuple(
                ExpectedEpisode(
                    episode_id=str(expected["episode_id"]),
                    max_rank=int(expected["max_rank"]),
                )
                for expected in item.get("expected", [])
            ),
            expected_empty=bool(item.get("expected_empty", False)),
        )
        for item in raw
    )


def _queries(plan: dict[str, object]) -> list[EpisodeRecallQuery]:
    queries: list[EpisodeRecallQuery] = []
    for raw_unit in plan.get("intent_units", []):
        if not isinstance(raw_unit, dict):
            continue
        unit_id = str(raw_unit.get("id") or "")
        for priority, expression in enumerate(raw_unit.get("recall_queries") or []):
            queries.append(
                EpisodeRecallQuery(
                    str(expression),
                    (unit_id,) if unit_id else (),
                    priority,
                )
            )
    return queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--min-recall", type=float, default=1.0)
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    cases = _load_cases(args.labels.expanduser().resolve())
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    store = Store(database)
    checks = 0
    passed = 0
    case_passed = 0
    reciprocal_rank = 0.0

    for case in cases:
        row = connection.execute(
            """SELECT plan_json FROM context_plans
               WHERE turn_id=? ORDER BY revision DESC LIMIT 1""",
            (case.turn_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"benchmark Turn is missing: {case.name}")
        plan = json.loads(str(row["plan_json"]))
        bound = {
            str(item["episode_id"])
            for item in connection.execute(
                "SELECT episode_id FROM episode_turns WHERE turn_id=?",
                (case.turn_id,),
            )
        }
        hits = [
            item
            for item in store.search_episode_queries(
                _queries(plan), max(args.top, 12)
            )
            if str(item["id"]) not in bound
        ][: args.top]
        ranks = {str(item["id"]): rank for rank, item in enumerate(hits, 1)}
        failures: list[str] = []
        if case.expected_empty:
            checks += 1
            if hits:
                failures.append("expected no recalled Episode")
            else:
                passed += 1
        for expected in case.expected:
            checks += 1
            rank = ranks.get(expected.episode_id)
            if rank is None or rank > expected.max_rank:
                failures.append(f"expected result missing from top {expected.max_rank}")
            else:
                passed += 1
                reciprocal_rank += 1.0 / rank
        if not failures:
            case_passed += 1
        print(f"{'PASS' if not failures else 'FAIL'} {case.name}")
        print(
            "  actual="
            + ", ".join(
                f"{rank}:score={item['search_score']:.3f}:"
                f"confidence={item['relevance_confidence']:.3f}"
                for rank, item in enumerate(hits, 1)
            )
        )
        for failure in failures:
            print(f"  expected={failure}")

    recall = passed / checks if checks else 1.0
    mean_reciprocal_rank = reciprocal_rank / max(
        1, sum(len(case.expected) for case in cases)
    )
    print(
        f"SUMMARY cases={case_passed}/{len(cases)} checks={passed}/{checks} "
        f"recall={recall:.3f} mrr={mean_reciprocal_rank:.3f}"
    )
    store.close()
    connection.close()
    successful = recall >= args.min_recall and case_passed == len(cases)
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
