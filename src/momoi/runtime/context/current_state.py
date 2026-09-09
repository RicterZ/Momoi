"""Live state is attached to current input, never to historical transcripts."""

from xml.sax.saxutils import escape, quoteattr

from ...storage import Store
from ..turn_support import pack_user_context


CURRENT_STATE_STAGES = frozenset(
    {"owner", "goal", "heartbeat", "webhook", "reply_followup"}
)


def pack_current_turn_context(
    store: Store,
    stage: str,
    *items: tuple[str, str],
    include_empty: bool = False,
) -> str:
    if stage not in CURRENT_STATE_STAGES:
        raise ValueError(f"current state is not available in {stage}")
    snapshot = store.current_state.snapshot()
    slots = []
    for slot in snapshot.slots:
        attributes = {
            "id": slot.id,
            "subject": slot.subject,
            "key": slot.key,
            "expires_at": store.context_timestamp(slot.expires_at),
        }
        rendered = "".join(
            f" {key}={quoteattr(value)}" for key, value in attributes.items()
        )
        slots.append(f"<slot{rendered}>{escape(slot.value)}</slot>")
    packed = pack_user_context(("current_state", "\n".join(slots)), *items)
    # Mid-Turn owner updates must explicitly invalidate a prior nonempty snapshot.
    if include_empty and not slots:
        return "<current_state />\n\n" + packed
    return packed
