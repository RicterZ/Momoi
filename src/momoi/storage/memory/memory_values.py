from __future__ import annotations

from collections.abc import Mapping
from xml.sax.saxutils import escape, quoteattr

from ...memory.retrieval.models import MemoryRecallQuery
from ...memory.storage.records import MEMORY_ACTIVATIONS, MEMORY_KINDS, memory_snapshot_fingerprint
from ...memory.text import estimate_tokens, truncate_tokens, token_chunk
from ...memory.metadata import TagCatalog
from ...memory.writing.validation import ALWAYS_MEMORY_KINDS, valid_scoped_memory_key

MOMOI_MEMORY_TAGS = TagCatalog({
    "food_drink": "饮食、饮品、口味及相关限制",
    "health": "健康、身体状况与长期健康习惯",
    "work_study": "工作、学习及相关安排和方法",
    "technology": "软件、设备、编程和技术偏好",
    "travel": "出行、地点及旅行偏好",
    "leisure": "游戏、音乐、阅读等休闲活动",
    "daily_life": "作息、居住、生活习惯",
    "social": "家人、朋友、其他人和人际关系",
    "communication": "称呼、表达方式、互动边界",
})


REFLECTION_MEMORY_CAUTION = (
    "Daily reflection memories are fallible and may be outdated or no longer "
    "applicable; use them only as supporting context and prefer current evidence."
)

def format_memory(row: Mapping[str, object]) -> str:
    attributes = {"id": row["id"], "kind": row["kind"], "key": row["key"]}
    if row.get("activation"):
        attributes["activation"] = row["activation"]
    header = " ".join(
        f"{key}={quoteattr(str(value))}" for key, value in attributes.items()
    )
    return f"<memory {header}>{escape(str(row['content']))}</memory>"


def format_reflection_memory(row: Mapping[str, object]) -> str:
    attributes = {
        "date": row.get("local_date"),
        "confidence": row.get("confidence"),
    }
    header = " ".join(
        f"{key}={quoteattr(str(value))}"
        for key, value in attributes.items()
        if value is not None
    )
    lines = [f"<reflection {header}>", f"  <content>{escape(str(row['content']))}</content>"]
    if row.get("evidence"):
        lines.append(f"  <evidence>{escape(str(row['evidence']))}</evidence>")
    lines.append("</reflection>")
    return "\n".join(lines)
