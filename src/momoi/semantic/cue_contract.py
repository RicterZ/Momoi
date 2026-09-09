"""Shared retrieval intent for archived cues and current memory queries."""

EVENT_RETRIEVAL_CONTRACT = (
    "Use the conversation's language to express the retrieval scenario, intent, and "
    "related content, people, tasks, or keywords. Preserve names and identifiers that "
    "help distinguish topics. Do not invent facts or connect unrelated events. "
)

CUE_ARCHIVE_CONTRACT = EVENT_RETRIEVAL_CONTRACT + (
    "Return 3-5 query-like passages in recall_cues for later semantic matching. "
    "Consider when this information might be needed and which content, people, "
    "tasks, or keywords it relates to. Write each text as a short natural-language "
    "sentence expressing a matchable scenario, intent, or related content. "
    "Describe possible future retrieval needs; do not split the summary into a fact "
    "list or repeat the full answer in the query. Cover distinct retrieval angles "
    "without redundant paraphrases to meet a quota; return fewer than three cues "
    "or none when information is insufficient. evidence_message_ids must reference "
    "retained messages that can satisfy the retrieval need. The future query scenario "
    "need not have occurred, but its factual premises must have sources. Preserve "
    "attribution, corrections, negation, and the distinction between dreams, plans, "
    "and actual events."
)

CUE_QUERY_CONTRACT = EVENT_RETRIEVAL_CONTRACT + (
    "Based on the current conversation, write a natural-language query describing "
    "the historical information needed now. Archived cues describe future scenarios "
    "in which a memory might be needed; the current query expresses that need itself. "
    "Both are matched semantically through the same scenarios, intents, and related "
    "content. Use only known context to narrow the query and resolve references. "
    "Do not guess unknown answers; knowing the original cue wording is unnecessary. "
    "The query searches both episode summaries and individually embedded CUES."
)
