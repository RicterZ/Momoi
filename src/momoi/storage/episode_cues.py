"""Episode retrieval labels and their links to verified summary evidence."""
from __future__ import annotations

import json
from collections.abc import Sequence


def cue_texts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        text.strip()
        for value in values
        if isinstance(text := value.get("text") if isinstance(value, dict) else value, str)
        and text.strip()
    ]


def stored_cue_texts(raw: str) -> list[str]:
    return cue_texts(json.loads(raw))


def normalize_cues(values: object, claims: Sequence[dict[str, object]]) -> list[object]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("invalid episode recall cues")
    evidence_ids = {claim["message_id"] for claim in claims}
    normalized: dict[str, object] = {}
    for value in values:
        # Read/write compatibility for labels created before evidence links.
        legacy = isinstance(value, str)
        if not legacy and (
            not isinstance(value, dict)
            or set(value) != {"text", "evidence_message_ids"}
        ):
            raise ValueError("invalid episode recall cue structure")
        text = value if legacy else value["text"]
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 100:
            raise ValueError("invalid episode recall cue text")
        text = " ".join(text.split())
        if legacy:
            normalized.setdefault(text.casefold(), text)
            continue
        ids = value["evidence_message_ids"]
        if (
            not isinstance(ids, list) or not ids
            or any(type(id) is not int or id not in evidence_ids for id in ids)
        ):
            raise ValueError("cue must link to retained verified claims")
        normalized.setdefault(text.casefold(), {
            "text": text, "evidence_message_ids": list(dict.fromkeys(ids)),
        })
    return list(normalized.values())
