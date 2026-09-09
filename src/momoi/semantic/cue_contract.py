"""Shared event representation for archived cues and retrieval queries."""

EVENT_RETRIEVAL_CONTRACT = (
    "Describe one distinguishable event or ongoing concern in self-contained, neutral language. "
    "Identify participants as OWNER and ASSISTANT; preserve literal names, identifiers, "
    "the object, action or relationship, and supported distinguishing circumstances. "
    "Preserve attribution, uncertainty, negation, corrections and whether an action is "
    "a dream, proposal, attempt or reported completion. Do not invent missing details "
    "or connect unrelated events merely because they share an entity or keyword. "
    "Use the conversation language; do not replace an event with generic topic tags. "
)

CUE_ARCHIVE_CONTRACT = EVENT_RETRIEVAL_CONTRACT + (
    "For archived cues, use only retained evidence and link every cue to supporting "
    "message IDs. A reported event is not independently verified. Each cue must make "
    "sense when embedded alone; retain the corrected account instead of superseded facts."
)

CUE_QUERY_CONTRACT = EVENT_RETRIEVAL_CONTRACT + (
    "For retrieval, describe the missing information rather than inventing its answer. "
    "Known details constrain the search; unknown details remain unknown. Resolve pronouns "
    "from supplied context only. Exact stored cue wording need not be known. This semantic "
    "query searches both episode summaries and independently embedded event cues."
)
