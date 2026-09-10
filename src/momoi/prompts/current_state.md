Maintain current state. A state is a condition or ongoing situation that remains
true beyond the source Turn and may matter later. A momentary or completed event
is not a state, however recent or noteworthy it is.

- On every pass, review all existing slots and delete anything that is not a
  state, has ended, or is no longer useful.
- Add only genuinely new states supported by the source Turn.
- Leave unchanged state out of the change set.
- When newer evidence supersedes or refines old state, delete conflicting or
  redundant slots and add one concise replacement.
- Do not renew or duplicate state merely because it was repeated or confirmed.
- Keep only the latest mutually compatible facts. Prefer one sentence per slot.
- Do not store dialogue, narrative detail, durable memory, Goal-owned plans, or
  inferences from mood, silence, and hypotheticals.

Return empty `add` and `delete` arrays when nothing materially changes.

Typical flow:
… → current_state_finish
