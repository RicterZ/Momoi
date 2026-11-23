"""Reconstruct candidate calibration samples from historical memory exports."""

import copy
import json


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
