"""Shared wire formatting for observed speech and runtime records."""
import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from ...storage.core.timestamps import context_timestamp
from .models import TranscriptGroup
from ...storage.delivery.actions import QQ_POKE_MARKER

def render_bubble(
    text: str,
    *,
    delivery_state: str = "delivered",
    turn: str = "",
    time: str = "",
) -> str:
    if text == QQ_POKE_MARKER:
        text = "[QQ 动作：戳一戳用户]"
    attributes = ""
    if time:
        attributes += f" time={quoteattr(time)}"
    if turn:
        attributes += f" turn={quoteattr(turn)}"
    if delivery_state == "queued":
        attributes += ' delivery="queued"'
    return f"<bubble{attributes}>\n{escape(text)}\n</bubble>"


def render_event(
    text: str, identifier: int, source: str, received_at: float, timezone: ZoneInfo
) -> str:
    timestamp = datetime.fromtimestamp(received_at, timezone).isoformat(timespec="seconds")
    if source == "napcat:message_recall":
        text = text.replace("\n这条消息已撤回，不再作为当前请求或待执行指令；已执行的操作不代表已经回滚。", "")
    source_attr = "" if source == "napcat:message_recall" else f" source={quoteattr(source)}"
    return (
        f'<event id="E{identifier}"{source_attr} received_at="{timestamp}">\n'
        f'{escape(text)}\n</event>'
    )


def render_review(
    kind: str, text: str, identifier: int, completed_at: float, timezone: ZoneInfo
) -> str:
    timestamp = datetime.fromtimestamp(completed_at, timezone).isoformat(timespec="seconds")
    if kind == "plan_step":
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            record = {"result": text}
        if not isinstance(record, dict):
            record = {"result": text}
        root = Element("plan_step", id=f"P{identifier}", completed_at=timestamp)
        for key in ("plan_id", "step_id", "status"):
            if key in record:
                root.set(key, str(record[key]))
        SubElement(root, "result").text = str(record.get("result", ""))
        outputs = SubElement(root, "outputs")
        for ref in record.get("output_refs", []):
            SubElement(outputs, "output", ref=str(ref))
        if "plan_status" in record:
            SubElement(root, "plan_status").text = str(record["plan_status"])
        return tostring(root, encoding="unicode")
    prefix = {"goal": "G", "heartbeat": "H", "plan_step": "P"}[kind]
    return (
        f'<{kind} id="{prefix}{identifier}" completed_at="{timestamp}">\n'
        f'{escape(text)}\n</{kind}>'
    )


def part_bubble(
    group: TranscriptGroup, index: int, timezone: ZoneInfo, turn: str = ""
) -> str:
    state = group.part_states[index] if index < len(group.part_states) else "delivered"
    moment = group.part_times[index] if index < len(group.part_times) else 0.0
    timestamp = context_timestamp(moment, timezone) if moment > 0 else ""
    return render_bubble(
        group.parts[index], delivery_state=state, turn=turn, time=timestamp
    )


def text_message(role: str, text: str) -> dict[str, object]:
    """Build a message in the block form both provider adapters already take.

    Historical owner messages will carry images and other media alongside their
    text, and the current request already uses blocks, so the transcript uses
    one shape throughout rather than mixing bare strings with block lists.
    """

    return {"role": role, "content": [{"type": "text", "text": text}]}

