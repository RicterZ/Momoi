Maintain only short-lived facts that are true now and useful to the next Turn.
Current state is a delta, not a transcript summary or complete snapshot.

- Add only genuinely new state.
- Leave unchanged state out of the change set.
- When newer evidence supersedes or refines old state, delete conflicting or
  redundant slots and add one concise replacement.
- Delete ended or no-longer-useful state without preserving its history.
- Do not renew or duplicate state merely because it was repeated or confirmed.
- Keep only the latest mutually compatible facts. Prefer one sentence per slot.
- Do not store completed events, dialogue, narrative detail, durable memory,
  Goal-owned plans, or inferences from mood, silence, and hypotheticals.

Return empty `add` and `delete` arrays when nothing materially changes.

Typical flow:
… → current_state_finish
