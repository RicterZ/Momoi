"""Bounded topic selection over metadata, independent of the chat transcript."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .structured_selection import SelectionProtocolError, select_structured
from ..storage.episode_cues import cue_texts
from ..observability.events import log_event

logger = logging.getLogger(__name__)
TOPIC_CANDIDATE_LIMIT = 8
SYSTEM = (Path(__file__).resolve().parents[1] / "prompts/topic_selection.md").read_text().strip()


async def select_topics(provider, store, request, queries, candidates, *, thinking_effort="low", diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(status="no_candidates", thinking_effort=thinking_effort or "model",
                       candidate_count=len(candidates), selected_ids=[], candidates=[], attempts=0, elapsed_ms=0)
    if not candidates:
        log_event(logger, logging.INFO, "topic_selection", **diagnostics)
        return []
    if len(candidates) > TOPIC_CANDIDATE_LIMIT:
        raise ValueError("too many topic candidates")
    payload = {
        "current_request": request,
        "retrieval_queries": [{"semantic": q.dense_expression, "keywords": q.expression}
                              for q in queries],
        "candidates": [{
            "index": i, "title": row["title"],
            "summary": row.get("narrative_summary") or row.get("summary") or "",
            "topics": row.get("topics") or [],
            "cues": cue_texts(row.get("recall_cues")),
            "conversation_time": store.topic_conversation_time(str(row["id"])),
        } for i, row in enumerate(candidates)],
    }
    diagnostics["queries"] = payload["retrieval_queries"]
    diagnostics["candidates"] = [{
        "episode_id": str(row["id"]), "title": row["title"],
        "prefilter_rank": i + 1, "score": row.get("search_score"),
        "cue_cosine": row.get("cue_cosine"), "channels": row.get("channels", []),
        "cues": cue_texts(row.get("recall_cues")),
        "cue_keyword_hit": any("recall_cue" in q.get("field_matches", [])
                               for q in row.get("matched_queries", [])),
    } for i, row in enumerate(candidates)]
    spec = {
        "name": "select_topics",
        "description": "Select relevant topic indices, best first; empty when none is relevant.",
        "input_schema": {"type": "object", "properties": {"indices": {
            "type": "array", "maxItems": TOPIC_CANDIDATE_LIMIT, "uniqueItems": True,
            "items": {"type": "integer", "minimum": 0, "maximum": len(candidates)-1},
        }}, "required": ["indices"], "additionalProperties": False},
    }

    def parse(args):
        indices = args.get("indices") if isinstance(args, dict) else None
        if (not isinstance(indices, list) or len(indices) > len(candidates)
                or any(type(i) is not int or not 0 <= i < len(candidates) for i in indices)
                or len(set(indices)) != len(indices)):
            raise SelectionProtocolError("Invalid or duplicate topic index")
        return [candidates[i] for i in indices]

    started = time.monotonic()
    try:
        selected, attempts = await select_structured(
            provider, SYSTEM, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            spec, parse, timeout=45, thinking_effort=thinking_effort,
        )
    except Exception as error:
        # Unfiltered broad candidates are not safe substitutes for selected topics.
        diagnostics.update(status="failed", error_type=type(error).__name__,
                           elapsed_ms=(time.monotonic()-started)*1000)
        log_event(logger, logging.WARNING, "topic_selection", **diagnostics)
        return []
    diagnostics.update(status="selected" if selected else "empty", attempts=attempts,
                       selected_ids=[str(row["id"]) for row in selected],
                       elapsed_ms=(time.monotonic()-started)*1000)
    log_event(logger, logging.INFO, "topic_selection", **diagnostics)
    return selected
