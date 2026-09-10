Maintain a small present-state scratchpad for the next Turn. It is not a
conversation summary, event log, Episode, or narrative of how the situation
developed.

Ask of every slot: "What is still true now, short-lived, and likely to change
how the next Turn understands or acts on the current situation?" If a detail
does not pass all three parts, omit it. Transcript and Episodes preserve past
events; memories preserve durable facts; Goals preserve scheduled work.

The output is a delta, not a complete snapshot. Do not resubmit untouched
slots. Compare the preceding committed Turn with the existing slots, using the
newest explicit evidence when evidence conflicts:

- Add a slot only for a newly established current fact.
- Preserve an existing slot without mentioning it when it remains current,
  useful, and compatible with the new evidence.
- Delete a slot when its state ended or is no longer useful as present context.
- When newer evidence contradicts, supersedes, or materially refines an
  existing state dimension, replace it atomically: delete every conflicting or
  redundant old slot and add exactly one freshly written successor if a useful
  current state remains. The old value is evidence to reassess, not text to
  extend or a template to copy. Never retain both old and new versions.
- Return empty add and delete arrays when the Turn only repeats, confirms, or
  paraphrases state already represented accurately. A duplicate is not a new
  state and does not justify renewing its TTL.

Keep at most one slot for the same live situation. This is deduplication, not a
request to merge its history. On every replacement, first discard all ended,
superseded, resolved, historical, duplicated, and next-Turn-irrelevant details;
then write only the minimal residue that is current, mutually compatible, and
useful now. A replacement should become shorter as details fall away. Never
preserve a detail merely because it appeared in the old slot.

Write each value as one compact current-state statement. Prefer one sentence.
Include a time only when a live deadline, duration, or temporal relation changes
the next Turn. Preserve attribution and uncertainty where they matter.

Do not record:

- completed steps, resolved conditions, prior dialogue, or the sequence of
  events that produced the current state;
- scene narration, flavor details, repeated character motifs, or emotional
  prose already available in the transcript;
- plans or reminders owned by a Goal, or durable facts owned by memory;
- inferences from mood, silence, hypothetical outcomes, or the mere presence of
  an existing slot.

Example state transitions:

- Existing: "Owner is at the station awaiting the 15:20 train."
- New evidence merely says they are still waiting: return empty arrays; do not
  duplicate or renew the slot.
- New evidence says they boarded: delete the waiting slot and add one current
  state such as "Owner is aboard the train to X." Do not retain the waiting
  state or recap lunch, packing, travel to the station, and boarding.
- New evidence says they arrived and no present fact remains useful: delete the
  travel slot without adding a successor.

Typical flow:
… → current_state_finish
