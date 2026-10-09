"""Calibrate write candidate limits from a read-only production JSON export.

The snapshot must contain memories, memory_tombstones, memory_operation_batches.
No production database is opened. Local BGE encodes text; no LLM is called.
Output contains private memory text and should remain outside the repository.
"""

import argparse
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from momoi.integrations.adapters.local_embedding import local_embedding
from momoi.memory.writing import candidates as module
from momoi.memory.writing.candidates import WriteCandidates
from momoi.memory.writing.models import PlanningContext
from momoi.memory.retrieval.models import DenseMemoryHit, DenseThresholds
from momoi.memory.retrieval.dense import VectorMemoryEvidence
from momoi.runtime.retrieval.models import CALIBRATION_PROFILES
from momoi.runtime.retrieval.service import QUERY_INSTRUCTION
from momoi.memory.text import estimate_tokens
from momoi.runtime.workflows.memory_operation.rendering import (
    render_memory_operation_request,
)
from momoi.policies import SemanticPolicy

LIMITS = (4, 8, 12, 16, 24, 32)


def record(m, tomb=None):
    m = copy.deepcopy(m)
    m["meta"] = json.loads(m.pop("meta_json") or "{}")
    m["meta"]["scope"] = m.pop("scope_key", "")
    m["superseded_by"] = None
    m.update(
        forgotten_at=tomb["created_at"] if tomb else None,
        forgotten_event_id=tomb["source_event_id"] if tomb else None,
        forgotten_quote=tomb["evidence_quote"] if tomb else None,
    )
    return m


def samples(data):
    ms = {m["id"]: m for m in data["memories"]}
    tombs = {
        (r["kind"], r["key"], r["scope_key"]): r for r in data["memory_tombstones"]
    }
    out = []
    for batch in data["memory_operation_batches"]:
        if batch.get("state", "completed") != "completed" or not batch.get("result_json"):
            continue
        requests = json.loads(batch["operations_json"])
        decisions = json.loads(batch["result_json"])
        t = batch["created_at"]
        corpus = []
        for m in ms.values():
            if m["created_at"] > t or (
                m["superseded_by"] in ms and ms[m["superseded_by"]]["created_at"] <= t
            ):
                continue
            tomb = tombs.get((m["kind"], m["key"], m["scope_key"]))
            if tomb and tomb["created_at"] > t:
                tomb = None
            if m["expires_at"] and m["expires_at"] <= t and not tomb:
                continue
            corpus.append(record(m, tomb))
        # Prefer captured contents for the versions visible at actual submission time.
        byid = {r["id"]: r for r in corpus}
        for row in json.loads(batch["context_json"]):
            if row["id"] in byid:
                for field in ("content", "key", "kind", "meta", "updated_at"):
                    if field in row:
                        byid[row["id"]][field] = row[field]
        for i, decision in enumerate(decisions):
            targets = decision.get("target_ids", [])
            if not targets:
                continue
            req = [r for r in requests if r["id"] in decision["operation_ids"]]
            if not set(targets) <= set(byid):
                continue
            out.append(
                dict(
                    name=f"history:{batch['sequence']}:{i}",
                    batch=batch["sequence"],
                    requests=req,
                    context=json.loads(batch["context_json"]),
                    targets=targets,
                    rows=corpus,
                    action=decision["action"],
                )
            )
    return out


async def main(args):
    ROOT = args.output
    ROOT.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.snapshot.read_text())
    cases = samples(data)
    if args.extra_cases:
        cases += json.loads(args.extra_cases.read_text())
    if not cases:
        raise ValueError("snapshot contains no historical target decisions")
    original_limit = module.PER_REQUEST
    expanded = []
    for case in cases:
        case["pool"] = "with_forgotten"
        expanded.append(case)
        valid = copy.deepcopy(case)
        valid["pool"] = "active_only"
        valid["rows"] = [r for r in valid["rows"] if r["forgotten_at"] is None]
        if set(valid["targets"]) <= {r["id"] for r in valid["rows"]}:
            expanded.append(valid)
    cases = expanded
    encoder = local_embedding()
    documents = {}
    queries = set()
    for case in cases:
        for row in case["rows"]:
            text = f"Kind: {row['kind']}\nKey: {row['key']}\nContent: {row['content']}"
            documents[text] = None
        queries.update(r["content"] for r in case["requests"])
    texts = list(documents) + [QUERY_INSTRUCTION + q for q in sorted(queries)]
    cache_path = ROOT / "vectors.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    missing = [text for text in texts if text not in cache]
    for offset in range(0, len(missing), 32):
        chunk = missing[offset : offset + 32]
        values = await encoder.encode(chunk, query=False)
        cache.update(zip(chunk, values))
    cache_path.write_text(json.dumps(cache))
    calibration = {
        "confirmed_memory": DenseThresholds(
            *CALIBRATION_PROFILES[encoder.space.calibration_profile]["confirmed_memory"]
        )
    }
    results = []
    policy = SemanticPolicy()
    for case in cases:
        rows = case["rows"]
        byid = {r["id"]: r for r in rows}
        matrix = np.asarray(
            [
                cache[f"Kind: {r['kind']}\nKey: {r['key']}\nContent: {r['content']}"]
                for r in rows
            ],
            dtype=np.float32,
        )
        similarities = {
            q: matrix @ np.asarray(cache[QUERY_INSTRUCTION + q], dtype=np.float32)
            for q in {r["content"] for r in case["requests"]}
        }

        async def dense(queries, limit, eligible_ids):
            hits = {}
            for q in queries:
                expression = q.dense_expression
                scores = similarities[expression]
                eligible = [
                    i
                    for i, r in enumerate(rows)
                    if str(r["id"]) in eligible_ids[expression]
                ]
                top = sorted(
                    eligible,
                    key=lambda i: (float(scores[i]), rows[i]["id"]),
                    reverse=True,
                )[: max(policy.candidate_floor, limit * policy.candidate_multiplier)]
                hits[expression] = {
                    ("confirmed_memory", str(rows[i]["id"])): DenseMemoryHit(
                        str(rows[i]["id"]), float(scores[i])
                    )
                    for i in top
                }
            return VectorMemoryEvidence(hits, calibration)

        collector = WriteCandidates(
            SimpleNamespace(planning_rows=lambda: copy.deepcopy(rows)),
            SimpleNamespace(dense_recall=dense),
        )
        for mode in ("retrieval", "host"):
            for limit in LIMITS:
                module.PER_REQUEST = limit
                req = copy.deepcopy(case["requests"])
                snapshots = {}
                if mode == "retrieval":
                    for r in req:
                        r.pop("target_id", None)
                else:
                    snapshots = {
                        r["id"]: copy.deepcopy(byid[r["id"]])
                        for r in case["context"]
                        if r["id"] in byid and byid[r["id"]]["forgotten_at"] is None
                    }
                    for r in snapshots.values():
                        for field in (
                            "forgotten_at",
                            "forgotten_event_id",
                            "forgotten_quote",
                        ):
                            r.pop(field, None)
                ctx = PlanningContext(req, {}, snapshots)
                try:
                    await collector.collect(ctx)
                except module.CandidateBudgetExceeded:
                    ctx.retrieval_fallback = "mandatory_budget_exceeded"
                ids = set(ctx.snapshots) | set(ctx.forgotten)
                found = set(case["targets"]) & ids
                xml = render_memory_operation_request(
                    now=0,
                    timestamp="replay",
                    operations=[],
                    visible={},
                    snapshots=ctx.snapshots,
                    evidence=[],
                    forgotten=ctx.forgotten,
                    retrieval_fallback=ctx.retrieval_fallback,
                )
                results.append(
                    dict(
                        name=case["name"],
                        pool=case["pool"],
                        guard_count=sum(i in ctx.forgotten for i in ids),
                        mode=mode,
                        k=limit,
                        targets=case["targets"],
                        found=sorted(found),
                        selected=sorted(ids),
                        all_found=len(found) == len(case["targets"]),
                        count=len(ids),
                        body_tokens=sum(
                            estimate_tokens(r["content"])
                            for r in list(ctx.snapshots.values())
                            + list(ctx.forgotten.values())
                        ),
                        rendered_tokens=estimate_tokens(xml),
                        fallback=ctx.retrieval_fallback,
                    )
                )
        case["target_cosines"] = {
            q: {str(i): float(scores[list(byid).index(i)]) for i in case["targets"]}
            for q, scores in similarities.items()
        }
    module.PER_REQUEST = original_limit
    (ROOT / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    (ROOT / "measurements.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    for pool in ("with_forgotten", "active_only"):
        for group in sorted({c["name"].split(":")[0] for c in cases}):
            for mode in ("retrieval", "host"):
                print(pool, group, mode)
                for k in LIMITS:
                    rs = [
                        r
                        for r in results
                        if r["pool"] == pool
                        and r["k"] == k
                        and r["mode"] == mode
                        and r["name"].startswith(group + ":")
                    ]
                    print(
                        k,
                        "all",
                        sum(r["all_found"] for r in rs),
                        "/",
                        len(rs),
                        "targets",
                        sum(len(r["found"]) for r in rs),
                        "/",
                        sum(len(r["targets"]) for r in rs),
                        "mean size",
                        round(np.mean([r["count"] for r in rs]), 1),
                        "rendered tokens avg/p95",
                        round(np.mean([r["rendered_tokens"] for r in rs])),
                        round(np.percentile([r["rendered_tokens"] for r in rs], 95)),
                        "fallbacks",
                        sum(bool(r["fallback"]) for r in rs),
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extra-cases", type=Path)
    asyncio.run(main(parser.parse_args()))
