"""Live evaluation of the Episode relation review on fictional cases.

recall always returns the case's fixed candidates, so only the relation judgment
is measured. Usage:

    uv run --locked --env-file .env.evals python -m evals.episode_relations --repeat 3
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import tempfile

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
from momoi.runtime.workflows.episode.relation_contracts import FINISH_SPEC, SYSTEM, render_source

from .runner import TEMP_ROOT, model_config

DATASET = Path(__file__).parent / "datasets" / "episode_relations.json"
MAX_ROUNDS = 6


def _record(item):
    return {"id": item["id"], "title": item["title"], "summary": item["summary"],
            "turns": [{"role": role, "content": text} for role, text in item["turns"]]}


def _texts(record):
    yield record["title"]
    yield record["summary"]
    for role, text in record.get("turns") or record.get("conversation") or ():
        yield text


def _validate(case, relations):
    candidates = {item["id"]: item for item in case["candidates"]}
    seen = set()
    for index, item in enumerate(relations):
        target = candidates.get(item.get("target_episode_id"))
        if target is None or target["id"] in seen:
            return f"relations[{index}] 目标不在本次 recall 结果中或重复"
        seen.add(target["id"])
        if not any(item.get("source_evidence", "") in text for text in _texts(case["source"])):
            return f"relations[{index}].source_evidence 未出现在当前话题原文中"
        if not any(item.get("target_evidence", "") in text for text in _texts(target)):
            return f"relations[{index}].target_evidence 未出现在召回话题 {target['id']} 原文中"
    return None


async def run_case(provider, case):
    source = case["source"]
    episode = {"id": f"ep_{case['id']}_current", "title": source["title"], "narrative_summary": source["summary"]}
    messages = [{"id": f"m{index}", "role": role, "timestamp": f"2026-01-01T12:{index:02d}:00+08:00",
                 "content": text} for index, (role, text) in enumerate(source["conversation"])]
    transcript = [{"role": "user", "content": render_source(episode, messages)}]
    episodes = [_record(item) for item in case["candidates"]]
    recalled = False
    for _ in range(MAX_ROUNDS):
        response = await provider.complete(SYSTEM, transcript, [RECALL_TOOL_SPEC, FINISH_SPEC], require_tool=True)
        transcript.append({"role": "assistant", "content": response.content,
                           "provider_continuation": response.continuation})
        results = []
        for call in response.tool_calls:
            if call.name == "recall":
                recalled = True
                result = {"ok": True, "episodes": episodes}
            elif call.name == "episode_relation_finish":
                relations = call.arguments.get("relations")
                error = ("recall_required" if not recalled else
                         "invalid_relations" if not isinstance(relations, list) else _validate(case, relations))
                if error is None:
                    return relations
                result = {"ok": False, "error": "invalid_relations", "message": error}
            else:
                result = {"ok": False, "error": "tool_not_allowed"}
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False)})
        if not results:
            results = [{"type": "text", "text": "Call recall, then episode_relation_finish."}]
        transcript.append({"role": "user", "content": results})
    raise RuntimeError("relation review did not finish")


def score(rows):
    counts = Counter()
    errors = Counter()
    for row in rows:
        if row.get("error"):
            counts["errors"] += 1
            continue
        for pair in row["pairs"]:
            predicted, label = pair["predicted"], pair["label"]
            counts["pairs"] += 1
            counts["correct"] += predicted in pair["accept"]
            counts["expected_follows_up"] += label == "follows_up"
            counts["predicted_follows_up"] += predicted == "follows_up"
            counts["accepted_follows_up"] += predicted == "follows_up" and predicted in pair["accept"]
            counts["recalled_follows_up"] += predicted == label == "follows_up"
            if label == "none":
                counts["negative_pairs"] += 1
                counts["negative_linked"] += predicted not in pair["accept"]
                counts["negative_follows_up"] += predicted == "follows_up"
            if predicted not in pair["accept"]:
                errors[f"{pair['category']}:{label}->{predicted}"] += 1

    def ratio(a, b):
        return round(counts[a] / counts[b], 4) if counts[b] else None
    return {
        "runs": len(rows), "errors": counts["errors"], "pairs": counts["pairs"],
        "pair_accuracy": ratio("correct", "pairs"),
        "follows_up_precision": ratio("accepted_follows_up", "predicted_follows_up"),
        "follows_up_recall": ratio("recalled_follows_up", "expected_follows_up"),
        "negative_follows_up_rate": ratio("negative_follows_up", "negative_pairs"),
        "negative_wrong_edge_rate": ratio("negative_linked", "negative_pairs"),
        "mistakes": dict(errors.most_common()),
    }


async def execute(args):
    cases = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    output = Path(tempfile.mkdtemp(prefix="momoi-relation-eval-", dir=TEMP_ROOT))
    semaphore = asyncio.Semaphore(args.concurrency)
    async with OpenAIProvider(model_config("MOMOI_EVAL")) as provider:
        async def one(case, attempt):
            async with semaphore:
                row = {"case": case["id"], "attempt": attempt}
                try:
                    relations = await run_case(provider, case)
                except Exception as error:
                    return {**row, "error": type(error).__name__}
                chosen = {item["target_episode_id"]: item for item in relations}
                row["pairs"] = [{
                    "target": item["id"], "category": item["category"], "label": item["label"],
                    "accept": item.get("accept", [item["label"]]),
                    "predicted": chosen.get(item["id"], {}).get("relation", "none"),
                    "explanation": chosen.get(item["id"], {}).get("explanation", ""),
                } for item in case["candidates"]]
                return row
        rows = await asyncio.gather(*(one(case, attempt) for attempt in range(args.repeat) for case in cases))
    with (output / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = score(rows)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**summary, "output": str(output)}, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--case", action="append")
    asyncio.run(execute(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
