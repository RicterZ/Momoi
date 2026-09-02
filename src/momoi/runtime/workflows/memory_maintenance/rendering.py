import json
from collections.abc import Mapping, Sequence

from ....storage import memory_snapshot_fingerprint


def _memory_block(memory: Mapping[str, object], *, compact: bool = False) -> str:
    content = " ".join(str(memory.get("content") or "").split())
    if compact and len(content) > 240:
        content = content[:237].rstrip() + "..."
    fields = [
        f"memory_id={memory['id']}",
        f"kind={memory.get('kind') or 'unknown'}",
        f"key={memory.get('key') or 'unknown'}",
        f"activation={memory.get('activation') or 'unknown'}",
    ]
    if not compact:
        fields.extend(
            [
                f"snapshot_fingerprint={memory_snapshot_fingerprint(memory)}",
                f"updated_at={memory.get('updated_at') or 'unknown'}",
                f"expires_at={memory.get('expires_at') or 'none'}",
                f"evidence_quote={json.dumps(str(memory.get('evidence_quote') or ''), ensure_ascii=False)}",
            ]
        )
    return " ".join(fields) + "\ncontent=" + content


def render_memory_maintenance_request(
    *,
    mutable_memories: Sequence[Mapping[str, object]],
    context_memories: Sequence[Mapping[str, object]],
    memory_directory: Sequence[Mapping[str, object]],
    owner_evidence: Sequence[Mapping[str, object]],
    topic_context: str = "",
) -> str:
    def section(name: str, body: str) -> str:
        return f"<{name}>\n{body or '(none)'}\n</{name}>"

    evidence = "\n\n".join(
        f"event_id={item.get('event_id')} at={item.get('occurred_at') or 'unknown'}\n"
        f"{item.get('content') or ''}"
        for item in owner_evidence
    )
    return "\n\n".join(
        (
            section(
                "mutable_memories",
                "\n\n".join(_memory_block(item) for item in mutable_memories),
            ),
            section(
                "context_memories",
                "\n\n".join(_memory_block(item) for item in context_memories),
            ),
            section(
                "memory_directory",
                "\n".join(
                    _memory_block(item, compact=True) for item in memory_directory
                ),
            ),
            section("owner_evidence", evidence),
            section("topic_context", topic_context),
        )
    )
