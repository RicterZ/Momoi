"""Bounded topic selection over metadata, independent of the chat transcript."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .structured_selection import SelectionProtocolError, select_structured
from ..storage.episode_cues import cue_texts

logger = logging.getLogger(__name__)
TOPIC_CANDIDATE_LIMIT = 8
SYSTEM = (Path(__file__).resolve().parents[1] / "prompts/topic_selection.md").read_text().strip()


async def select_topics(provider, store, request, queries, candidates):
    if not candidates:
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
            spec, parse, timeout=45,
        )
    except Exception:
        # Unfiltered broad candidates are not safe substitutes for selected topics.
        logger.exception("Topic selection failed; returning no newly recalled topics")
        return []
    logger.info("Topic selection candidates=%d selected=%d attempts=%d elapsed_ms=%.0f effort=low",
                len(candidates), len(selected), attempts, (time.monotonic()-started)*1000)
    return selected
