#!/usr/bin/env python3
"""Dump the Owner request the current assembly would send, from a real database.

Rebuilds one historical Owner Turn offline so the request shape can be read
without running the daemon or calling a provider. Both forms are written: the
logical messages the runtime builds, and the Anthropic wire form after adjacent
same-role messages are merged for that API's alternation rule.

The database must be a copy. Nothing is written back to it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from momoi.llm.anthropic import merge_adjacent_roles
from momoi.llm.openai import openai_messages
from momoi.runtime.context.rendering import assemble_main_context
from momoi.runtime.transcript.building import build_transcript
from momoi.runtime.turn_support import (
    STYLE_CARD_SYSTEM_PROMPT,
    owner_content_blocks,
    owner_context_message,
    pack_user_context,
    sections,
)
from momoi.storage import Store

CONTRACT = Path(__file__).resolve().parents[1] / "src/momoi/prompts/system.md"


def system_blocks(store: Store, soul: Path | None) -> list[dict[str, object]]:
    text = (
        CONTRACT.read_text(encoding="utf-8")
        .replace(
            "{{SOUL}}",
            soul.read_text(encoding="utf-8")
            if soul and soul.exists()
            else "No additional Soul is configured.",
        )
        .replace("{{STYLE_CARD}}", STYLE_CARD_SYSTEM_PROMPT)
        .replace("{{CAPABILITY_POLICIES}}", "")
    )
    blocks: list[dict[str, object]] = [
        {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
    ]
    emotions = store.emotion_context()
    if emotions.strip():
        blocks.append(
            {
                "type": "text",
                "text": sections(("emotion_catalog", emotions)),
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks


def message_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text") or "")
        for block in content or []
        if isinstance(block, dict)
    )


def multimodal_example() -> dict[str, object]:
    """Show the tail layout for a batch that mixes text and an attachment.

    Stored history keeps only the text projection of a picture, so a real Turn
    cannot demonstrate this. The blocks below are synthetic and labelled as
    such; only their arrangement matters.
    """

    class Event:
        def __init__(self, text: str, image: bool) -> None:
            self.text = text
            self.occurred_at = 1_756_659_000.0
            self.segments = ({"type": "image"},) if image else ({"type": "text"},)

    def content_blocks(segments: tuple[dict[str, object], ...]):
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "<base64 elided>",
                },
            }
            for segment in segments
            if segment.get("type") == "image"
        ]

    batch = [Event("看这个", False), Event("", True), Event("怎么样", False)]
    logical = [{"role": "user", "content": owner_content_blocks(batch, content_blocks)}]
    return {
        "note": "synthetic; demonstrates attachment placement only",
        "logical": logical,
        "anthropic_wire": merge_adjacent_roles(logical),
        "openai_wire": openai_messages("", logical),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--soul", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/momoi-owner-request.json"))
    parser.add_argument("--offset", type=int, default=0, help="0 is the newest Owner Turn")
    parser.add_argument("--transcript-turns-min", type=int, default=48)
    parser.add_argument("--transcript-turns-max", type=int, default=96)
    parser.add_argument("--max-input-tokens", type=int, default=142222)
    parser.add_argument("--context-compaction-ratio", type=float, default=0.9)
    parser.add_argument("--summary-tokens", type=int, default=6000)
    args = parser.parse_args()

    store = Store(args.database)
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    subject = connection.execute(
        """SELECT t.id AS turn_id, t.started_at AS started_at, m.content AS owner_text,
                  (SELECT p.retrieval_json FROM context_plans AS p
                   WHERE p.turn_id=t.id ORDER BY p.revision DESC LIMIT 1) AS retrieval
           FROM turns AS t
           JOIN messages AS m ON m.turn_id=t.id AND m.role='user'
           WHERE t.kind='owner' AND t.state='completed'
           ORDER BY t.started_at DESC LIMIT 1 OFFSET ?""",
        (args.offset,),
    ).fetchone()
    if subject is None:
        print("no Owner Turn at that offset", file=sys.stderr)
        return 1

    turn_limit = store.transcript_window_turn_limit(
        args.transcript_turns_min,
        args.transcript_turns_max,
    )
    conversation_rows = store.recent_conversation_messages(
        turn_limit,
        round(args.max_input_tokens * args.context_compaction_ratio),
        float(subject["started_at"]),
    )
    transcript = build_transcript(
        conversation_rows,
        tool_activity=store.turn_activity(
            [str(row["turn_id"]) for row in conversation_rows]
        ),
    )
    recalled = assemble_main_context(
        store,
        json.loads(str(subject["retrieval"] or "{}")),
        args.summary_tokens,
    )
    context_message = owner_context_message(
        ("long_term_memories", recalled["long_term_memories"]),
        ("recent_memories", recalled["recent_memories"]),
        ("goal_directory", recalled["goal_directory"]),
    )
    runtime_text = pack_user_context(
        ("goal_progress", recalled["goal_progress"]),
        ("recall_memories", recalled["recall_memories"]),
        ("recall_status", recalled["query_recall"]),
        ("reflection_memories", recalled["reflection_memories"]),
        ("episode_directory", recalled["episodes"]),
        ("recent_external_events", recalled["recent_external_events"]),
    )
    tail_content = owner_content_blocks(
        [
            SimpleNamespace(
                occurred_at=float(subject["started_at"]),
                text=str(subject["owner_text"]),
                segments=(),
            )
        ],
        lambda _segments: [],
        runtime_text,
    )
    tail_content[-1]["cache_control"] = {"type": "ephemeral"}
    tail = {"role": "user", "content": tail_content}
    logical = [
        *([context_message] if context_message else []),
        *transcript.messages,
        tail,
    ]
    system = system_blocks(store, args.soul)
    payload = {
        "turn_id": str(subject["turn_id"]),
        "system": system,
        "messages": logical,
        "anthropic_wire_messages": merge_adjacent_roles(logical),
        "openai_wire_messages": openai_messages("", logical),
        "orphaned_proactive_groups": [
            {"turn_ids": list(group.turn_ids), "bubbles": list(group.parts)}
            for group in transcript.orphaned
        ],
        "synthetic_multimodal_tail": multimodal_example(),
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def roles(messages: list[dict[str, object]]) -> str:
        return " ".join(str(message["role"])[0] for message in messages)

    print(
        json.dumps(
            {
                "written": str(args.output),
                "turn": str(subject["turn_id"])[:8],
                "system_block_chars": [
                    len(str(block["text"])) for block in payload["system"]
                ],
                "logical_messages": len(logical),
                "logical_roles": roles(logical),
                "wire_messages": len(payload["anthropic_wire_messages"]),
                "wire_roles": roles(payload["anthropic_wire_messages"]),
                "owner_silence_markers": sum(
                    1 for message in logical if "did not reply" in message_text(message)
                ),
                "momoi_silence_markers": sum(
                    1
                    for message in logical
                    if "without replying" in message_text(message)
                ),
                "tool_call_lines": sum(
                    message_text(message).count("[tool_call]") for message in logical
                ),
                "orphaned_groups": len(transcript.orphaned),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    store.close()
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
