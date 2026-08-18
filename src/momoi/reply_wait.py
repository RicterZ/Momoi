import json


REPLY_WAIT_MIN_MINUTES = 1
REPLY_WAIT_MAX_MINUTES = 10
REPLY_FOLLOWUP_RETRY_SECONDS = 60


def encode_reply_wait(
    expected_information: str,
    reason: str,
    delay_minutes: int,
) -> str:
    return json.dumps(
        {
            "version": 1,
            "expected_information": expected_information.strip()[:300],
            "reason": reason.strip()[:500],
            "delay_minutes": max(
                REPLY_WAIT_MIN_MINUTES,
                min(REPLY_WAIT_MAX_MINUTES, int(delay_minutes)),
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_reply_wait(value: object) -> dict[str, object] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        item = json.loads(text)
    except json.JSONDecodeError:
        item = None
    if isinstance(item, dict) and item.get("version") == 1:
        expected = str(item.get("expected_information") or "").strip()
        reason = str(item.get("reason") or "").strip()
        delay = item.get("delay_minutes")
        if (
            expected
            and reason
            and isinstance(delay, int)
            and not isinstance(delay, bool)
            and REPLY_WAIT_MIN_MINUTES <= delay <= REPLY_WAIT_MAX_MINUTES
        ):
            return {
                "expected_information": expected[:300],
                "reason": reason[:500],
                "delay_minutes": delay,
            }
    return None
