"""Bounded topic selection over metadata, independent of the chat transcript."""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from .structured_selection import SelectionProtocolError, select_structured
from ..storage.episode.episode_cues import cue_texts
from ..observability.events import log_event

logger = logging.getLogger(__name__)
TOPIC_CANDIDATE_LIMIT = 8
SYSTEM = (Path(__file__).resolve().parents[1] / "prompts/topic_selection.md").read_text().strip()


@dataclass(frozen=True)
class RecallSelection:
    episodes: list[dict[str, object]]
    memories: list[dict[str, object]]
    reflections: list[dict[str, object]]


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

    for source, tag in (("memories", "memory_candidates"),
                        ("reflections", "reflection_candidates")):
        items = SubElement(root, tag)
        for value in payload.get(source) or []:
            if not isinstance(value, Mapping):
                continue
            candidate = SubElement(items, "candidate", {"index": str(value["index"])})
            _text(candidate, "kind", value.get("kind"))
            _text(candidate, "key", value.get("key"))
            _text(candidate, "content", value.get("content"))
            if source == "reflections":
                _text(candidate, "local_date", value.get("local_date"))
                _text(candidate, "confidence", value.get("confidence"))
                _text(candidate, "evidence", value.get("evidence"))

    return tostring(root, encoding="unicode")


async def select_topics(provider, store, request, queries, candidates, *, memory_candidates=(),
                        thinking_effort="low", diagnostics=None):
    memory_candidates = list(memory_candidates)
    confirmed = [row for row in memory_candidates if row.get("source") == "confirmed"]
    reflections = [row for row in memory_candidates if row.get("source") == "reflection"]
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(status="no_candidates", thinking_effort=thinking_effort or "model",
                       candidate_count=len(candidates), selected_ids=[], candidates=[],
                       memory_candidates=[], reflection_candidates=[], selected_memory_ids=[],
                       selected_reflection_ids=[], attempts=0, elapsed_ms=0)
    if not candidates and not confirmed and not reflections:
        log_event(logger, logging.INFO, "topic_selection", **diagnostics)
        return RecallSelection([], [], [])
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
        "memories": [{"index": i, "kind": row.get("kind"), "key": row.get("key"),
                      "content": row.get("content")} for i, row in enumerate(confirmed)],
        "reflections": [{"index": i, "kind": row.get("kind"), "key": row.get("key"),
                         "content": row.get("content"), "local_date": row.get("local_date"),
                         "confidence": row.get("confidence"), "evidence": row.get("evidence")}
                        for i, row in enumerate(reflections)],
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
    diagnostics["memory_candidates"] = [
        {"memory_id": row.get("id"), "kind": row.get("kind"), "key": row.get("key"),
         "prefilter_rank": i + 1, "score": row.get("search_score"),
         "channels": row.get("channels") or []}
        for i, row in enumerate(confirmed)
    ]
    diagnostics["reflection_candidates"] = [
        {"memory_id": row.get("id"), "kind": row.get("kind"), "key": row.get("key"),
         "prefilter_rank": i + 1, "score": row.get("search_score"),
         "confidence": row.get("confidence"), "channels": row.get("channels") or []}
        for i, row in enumerate(reflections)
    ]
    spec = {
        "name": "select_topics",
        "description": "Select relevant topic indices, best first; empty when none is relevant.",
        "input_schema": {"type": "object", "properties": {
            "indices": {"type": "array", "maxItems": len(candidates), "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 0, "maximum": max(0, len(candidates)-1)}},
            "memory_indices": {"type": "array", "maxItems": len(confirmed), "uniqueItems": True,
                               "items": {"type": "integer", "minimum": 0, "maximum": max(0, len(confirmed)-1)}},
            "reflection_indices": {"type": "array", "maxItems": len(reflections), "uniqueItems": True,
                                   "items": {"type": "integer", "minimum": 0, "maximum": max(0, len(reflections)-1)}},
        }, "required": ["indices", "memory_indices", "reflection_indices"],
            "additionalProperties": False},
    }

    def parse(args):
        if not isinstance(args, dict):
            raise SelectionProtocolError("selection arguments must be an object")
        selections = []
        for name, rows in (("indices", candidates), ("memory_indices", confirmed),
                           ("reflection_indices", reflections)):
            indices = args.get(name)
            if (not isinstance(indices, list) or len(indices) > len(rows)
                    or any(type(i) is not int or not 0 <= i < len(rows) for i in indices)
                    or len(set(indices)) != len(indices)):
                raise SelectionProtocolError(f"Invalid or duplicate {name}")
            selections.append([rows[i] for i in indices])
        return RecallSelection(*selections)

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
        return RecallSelection([], [], [])
    diagnostics.update(status="selected" if any((selected.episodes, selected.memories, selected.reflections)) else "empty", attempts=attempts,
                       selected_ids=[str(row["id"]) for row in selected.episodes],
                       selected_memory_ids=[int(row["id"]) for row in selected.memories],
                       selected_reflection_ids=[int(row["id"]) for row in selected.reflections],
                       elapsed_ms=(time.monotonic()-started)*1000)
    log_event(logger, logging.INFO, "topic_selection", **diagnostics)
    return selected
