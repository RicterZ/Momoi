from __future__ import annotations

from collections.abc import Mapping
from xml.sax.saxutils import escape, quoteattr

REFLECTION_MEMORY_CAUTION = (
    "Daily reflection memories are fallible and may be outdated or no longer "
    "applicable; use them only as supporting context and prefer current evidence."
)

def format_memory(row: Mapping[str, object]) -> str:
    attributes = {"id": row["id"], "kind": row["kind"], "key": row["key"]}
    if row.get("activation"):
        attributes["activation"] = row["activation"]
    meta = row.get("meta") or {}
    if meta.get("scope"):
        attributes["scope"] = meta["scope"]
    if meta.get("tags"):
        attributes["tags"] = ",".join(meta["tags"])
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
