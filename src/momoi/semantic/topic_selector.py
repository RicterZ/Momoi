"""Bounded topic selection over metadata, independent of the chat transcript."""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from .structured_selection import SelectionProtocolError, select_structured
from ..storage.episode.episode_cues import cue_texts
from ..observability.events import log_event

logger = logging.getLogger(__name__)
TOPIC_CANDIDATE_LIMIT = 8
SYSTEM = (Path(__file__).resolve().parents[1] / "prompts/topic_selection.md").read_text().strip()


def _text(parent: Element, tag: str, value: object) -> Element:
    node = SubElement(parent, tag)
    node.text = str(value or "")
    return node


def _text_list(parent: Element, tag: str, item_tag: str, values: object) -> None:
    node = SubElement(parent, tag)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return
    for value in values:
        _text(node, item_tag, value)


def render_topic_selection_request(payload: Mapping[str, object]) -> str:
    root = Element("topic_selection")
    _text(root, "current_request", payload.get("current_request"))

    queries = SubElement(root, "retrieval_queries")
    for value in payload.get("retrieval_queries") or []:
        if not isinstance(value, Mapping):
            continue
        query = SubElement(queries, "query")
        _text(query, "semantic", value.get("semantic"))
        _text(query, "keywords", value.get("keywords"))

    candidates = SubElement(root, "candidates")
    for value in payload.get("candidates") or []:
        if not isinstance(value, Mapping):
            continue
        candidate = SubElement(candidates, "candidate", {"index": str(value["index"])})
        _text(candidate, "title", value.get("title"))
        _text(candidate, "summary", value.get("summary"))
        _text_list(candidate, "topics", "topic", value.get("topics"))
        _text_list(candidate, "cues", "cue", value.get("cues"))
        conversation_time = value.get("conversation_time")
        if isinstance(conversation_time, Mapping):
            SubElement(
                candidate,
                "conversation_time",
                {
                    key: str(conversation_time[key])
                    for key in ("start", "end")
                    if conversation_time.get(key) not in (None, "")
                },
            )

    return tostring(root, encoding="unicode")


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
            provider,
            SYSTEM,
            [{"role": "user", "content": render_topic_selection_request(payload)}],
            spec, parse, timeout=45, thinking_effort=thinking_effort, stage="topic_selection",
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
