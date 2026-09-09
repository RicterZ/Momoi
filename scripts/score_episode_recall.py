#!/usr/bin/env python3
"""Score private, manually authored Episode qrels against saved Top-K results.

Usage: uv run python scripts/score_episode_recall.py --labels qrels.json \
    --results results-directory --variant rerank
Labels: {"qrels": [{"id": "q1", "best": "episode-id" or null,
"relevant": ["episode-id"], "grades": {"episode-id": 3, "other": 0}}]}.
Result files: <query-id>-<variant>.json with {"top": ["episode-id", ...]}.
Unjudged outputs cause an error; this script never assigns relevance labels.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def score(qrels: list[dict], results: dict[str, list[str]]) -> dict:
    answerable = sum(bool(q['relevant']) for q in qrels)
    positives = sum(len(set(q['relevant'])) for q in qrels)
    best = 0
    abstained = 0
    totals = {k: {'true_positive': 0, 'returned': 0} for k in (3, 8)}
    for q in qrels:
        top = results[q['id']]
        if len(set(top)) != len(top):
            raise ValueError(f"duplicate Episode output: {q['id']}")
        unknown = set(top) - set(q['grades'])
        if unknown:
            raise ValueError(f"unjudged outputs for {q['id']}: {sorted(unknown)}")
        relevant = set(q['relevant'])
        best += bool(top and q['best'] is not None and top[0] == q['best'])
        abstained += not relevant and not top
        for k, count in totals.items():
            count['true_positive'] += len(set(top[:k]) & relevant)
            count['returned'] += len(top[:k])
    result = {'queries': len(qrels), 'answerable': answerable,
              'positive_pairs': positives, 'best_top1_count': best,
              'best_top1': best / answerable if answerable else None,
              'abstention_correct': abstained,
              'abstention_total': len(qrels) - answerable}
    for k, count in totals.items():
        tp, returned = count['true_positive'], count['returned']
        result[f'at_{k}'] = {**count,
            'recall': tp / positives if positives else None,
            # Variable-length admission precision: denominator is actual output.
            'precision_returned': tp / returned if returned else None,
            'precision_fixed_slots': tp / (k * len(qrels)) if qrels else None}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--variant', required=True)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text())['qrels']
    results = {q['id']: json.loads((args.results / f"{q['id']}-{args.variant}.json").read_text())['top']
               for q in labels}
    print(json.dumps(score(labels, results), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
