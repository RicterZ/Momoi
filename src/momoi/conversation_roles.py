"""Identity-neutral speaker labels for archived conversation evidence."""


def speaker_label(role: object) -> str:
    value = str(role or "").strip().lower()
    return {"user": "OWNER", "assistant": "ASSISTANT"}.get(value, value.upper() or "UNKNOWN")
