Infer current state changes from the conversation records above for pending_turns. If evidence conflicts, use the latest Turn.

Maintain current state. State is short-term, TTL-backed working memory for temporary facts that would make your behavior wrong, right now, if forgotten — because they override a long-term default, or because they're a new temporary fact too immediate/short-lived for the 7-day recent memory or on-demand recall to reliably surface in time.

Test before adding: "If I forget this, will I say or do something wrong within its real-world time scope?" If no — skip it, no matter how notable or memorable. Weak reasons ("might be useful," "low risk," "could support continuity") never justify adding a slot.

Check memory before state: if the fact is durable — a rule, preference, relationship, procedure, or cross-event state still worth knowing after its moment passes — record it with `memory_operation` instead of adding a slot. State is only a short-term state or behavior that can change or be forgotten within its real-world scope.

Examples:
* "Took a taxi to work today" — overrides long-term default "commutes by bike." Ends at midnight.
* "Napping until 2pm" — new temporary fact, not a default override. Delete early if person says they can't sleep.
* "Off work today" — overrides default work schedule. Ends at midnight.
* Anti-example: person jokingly calls you a nickname, teases you. Forgetting it breaks nothing — belongs to mood tracking, not state. Do not add.

Each slot = one concise current fact only. No dialogue, source, timestamps, narrative, or backstory. Normalize "At 8am they said they took a taxi" → "Today's commute mode: taxi."

Every pass: review and normalize all slots. Delete anything ended, conflicting, duplicated, or no longer meeting the test above. If a slot mixes fact with history, replace it with one clean version — never leave malformed content. Replace changed facts by deleting the old slot and adding one successor.

TTL = remaining real-world scope of the fact, not importance. Use explicit duration/date when given; unscoped "today" facts end at local midnight. Otherwise pick the shortest reasonable TTL for that fact's natural lifecycle — never the maximum just because it's uncertain or might matter later. If no clear TTL or ongoing applicability exists, don't add it. Delete early when new evidence ends it.

Default to exclusion when ambiguous. Return empty `add`/`delete` when nothing materially changes.
Typical flow: … → memory_operation? → current_state_finish
