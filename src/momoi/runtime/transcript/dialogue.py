"""Project completed reply exchanges from persisted delivery evidence."""
import json
from zoneinfo import ZoneInfo


def _payload(block):
    try:
        value = json.loads(block.get("content", ""))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def project_dialogue(exchanges, *, timezone: ZoneInfo, has_speech: bool, mark_silence: bool) -> None:
    """Edit replay copies only; keep live calls and durable journals unchanged.

    Replyer speech stays exclusively in the reply observation. Planner text,
    call arguments and business observations retain their original order.
    """
    ended = False
    for exchange in exchanges:
        content, results = exchange.get("content"), exchange.get("results")
        if not isinstance(content, list) or not isinstance(results, list):
            continue
        calls = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        outcomes = {block.get("tool_use_id"): block for block in results
                    if isinstance(block, dict) and block.get("type") == "tool_result"}
        removed = {call["id"] for call in calls if call.get("name") == "end_turn"
                   and _payload(outcomes.get(call.get("id"), {})).get("ok") is True}
        ended |= bool(removed)
        content = [block for block in content if not (
            isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") in removed
        )]
        results = [block for block in results if not (
            isinstance(block, dict) and block.get("tool_use_id") in removed
        )]
        calls = [call for call in calls if call.get("id") not in removed]
        evidence = exchange.get("_reply_messages", {})
        # An original accepted receipt can precede an asynchronous send failure.
        # Preserve that exchange, but expose the actual delivery observation.
        for call in calls:
            speech = evidence.get(call.get("id"), [])
            if call.get("name") == "reply" and any(row.get("delivery_state") == "failed" for row in speech):
                block = outcomes.get(call.get("id"))
                if block:
                    payload = _payload(block)
                    payload["delivery_state"] = "failed"
                    block["content"] = json.dumps(payload, ensure_ascii=False)
        exchange["content"], exchange["results"] = content, results
    if ended and not has_speech and mark_silence:
        exchanges.append({"content": [{"type": "text", "text": "[ended the Turn without replying]"}], "results": []})
