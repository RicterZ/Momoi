"""Fix private quotes of self-sent messages in the SnowLuma v2.2.0 bundle.

QQ quotes a self-sent private message by the low 16 bits of its client
sequence. v2.2.0 tries that value as a conversation sequence before matching
the quote time, so once the conversation passes that number the quote resolves
to an unrelated older message. Matching the time first keeps the sequence
lookup as a last resort.
"""

import sys
from pathlib import Path

ORIGINAL = """\t\t\tif (Number.isSafeInteger(replySequence) && replySequence > 0) {
\t\t\t\tconst byClientSequence = await this.findPrivateMessageId(sessionId, replySequence, sentBySelf, timestamp);
\t\t\t\tif (byClientSequence !== null) return byClientSequence;
\t\t\t\tconst byConversationSequence = await this.findPrivateMessageIdBySequence(sessionId, replySequence, sentBySelf);
\t\t\t\tif (byConversationSequence !== null) return byConversationSequence;
\t\t\t}
\t\t\tif (!sentBySelf) return null;
\t\t\tconst byExactTime = await this.findPrivateMessageIdAtTime(sessionId, true, timestamp, replyElements);
\t\t\tif (byExactTime !== null) return byExactTime;
\t\t\tconst byNearbyTime = await this.findUniquePrivateOutgoingNearTime(sessionId, timestamp);
\t\t\tif (byNearbyTime !== null) return byNearbyTime;
\t\t\treturn null;
"""

PATCHED = """\t\t\tconst usableSequence = Number.isSafeInteger(replySequence) && replySequence > 0;
\t\t\tif (usableSequence) {
\t\t\t\tconst byClientSequence = await this.findPrivateMessageId(sessionId, replySequence, sentBySelf, timestamp);
\t\t\t\tif (byClientSequence !== null) return byClientSequence;
\t\t\t}
\t\t\tif (sentBySelf) {
\t\t\t\tconst byExactTime = await this.findPrivateMessageIdAtTime(sessionId, true, timestamp, replyElements);
\t\t\t\tif (byExactTime !== null) return byExactTime;
\t\t\t\tconst byNearbyTime = await this.findUniquePrivateOutgoingNearTime(sessionId, timestamp);
\t\t\t\tif (byNearbyTime !== null) return byNearbyTime;
\t\t\t}
\t\t\tif (usableSequence) {
\t\t\t\tconst byConversationSequence = await this.findPrivateMessageIdBySequence(sessionId, replySequence, sentBySelf);
\t\t\t\tif (byConversationSequence !== null) return byConversationSequence;
\t\t\t}
\t\t\treturn null;
"""

HEADER = "\tasync resolvePrivateReplyMessageId(sessionId, replySequence, sentBySelf, timestamp, replyElements) {\n"


def patch(text: str) -> str:
    start = text.find(HEADER)
    if start < 0 or text.count(HEADER) != 1:
        raise SystemExit("resolvePrivateReplyMessageId not found exactly once; SnowLuma bundle changed")
    body = text.find(ORIGINAL, start)
    if body < 0 or body - start > 200 or text.count(ORIGINAL) != 1:
        raise SystemExit("resolvePrivateReplyMessageId body differs from v2.2.0; review the patch")
    return text[:body] + PATCHED + text[body + len(ORIGINAL):]


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_text(patch(path.read_text(encoding="utf-8")), encoding="utf-8")
