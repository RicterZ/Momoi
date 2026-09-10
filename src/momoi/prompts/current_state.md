Maintain current state. Current state is TTL-backed short-term working memory
that preserves facts within their real-world time scope when conversation
context is truncated.

A state is an evidence-supported, time-bounded fact about a person, object, or
situation. Keep it until its scope ends, even when the action that established
it has finished and regardless of whether the next Turn will use it. Afterward,
it must be safe to forget and must not imply that the state continues or recurs.

Examples:
- "I am off work today" lasts until today ends; never infer leave tomorrow.
- "I took a taxi to work today" records today's commute choice until today
  ends; never infer the same choice tomorrow.
- "I am going to nap for two hours" lasts at most two hours. If the person says
  an hour later that they cannot sleep, delete it immediately.

Every slot must contain only the concise current fact. Omit dialogue, source,
timestamps already represented by its scope, narrative sequence, and how the
state arose. For example, normalize "At 08:00 the person said they took a taxi
to work" to "Today's commute mode is taxi." A past event alone is not state
unless it establishes a temporary fact whose time scope is still active. Do not
store durable facts, artifacts, results, preferences, future Goals, speculation,
or anything merely because it might be mentioned later.

On every pass, review and normalize every existing slot. Delete anything that
is no longer a short-term state, has ended, conflicts with new evidence, or
duplicates another slot. If a slot mixes current state with history or other
detail, delete it and add one normalized replacement; never preserve malformed
content unchanged. Add only states supported by the source Turn. Replace changed
state by deleting the old slot and adding one concise successor. Leave only
valid, normalized, unchanged state untouched.

TTL is the remaining real-world scope of the fact, not how long it deserves to
be remembered. Use an explicit duration, date, or boundary when supplied;
"today" normally ends at local midnight. Otherwise choose the shortest
reasonable TTL from the ordinary lifecycle of that specific state. Never use
the maximum because a fact is important, uncertain, or might matter later. If
neither continued applicability nor a reasonable TTL is justified, do not add
the state. Delete it immediately when newer evidence ends it early.

Return empty `add` and `delete` arrays when nothing materially changes.

Typical flow:
… → current_state_finish
