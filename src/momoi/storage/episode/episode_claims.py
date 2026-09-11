import json
from collections.abc import Mapping, Sequence

from ...models import speaker_label


def render_verified_claims(claims: Sequence[Mapping[str, object]]) -> str:
    lines = []
    for claim in claims:
        source = speaker_label(claim["role"])
        if claim["role"] == "assistant":
            if claim["delivery_state"] == "uncertain":
                source += " delivery=uncertain"
            elif claim["delivery_state"] == "internal":
                source += " visibility=internal"
            else:
                source += " delivery=delivered"
        lines.append(
            f"- [source {source} turn={claim['turn_id']} "
            f"ordinal={claim['ordinal']}] "
            f"{json.dumps(claim['quote'], ensure_ascii=False)}"
        )
    return "\n".join(lines)
