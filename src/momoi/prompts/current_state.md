Maintain a small present-state scratchpad for the next Turn. It is not a
conversation summary, event log, Episode, or narrative of how the situation
developed.

Ask of every slot: "What is still true now, short-lived, and likely to change
how the next Turn understands or acts on the current situation?" If a detail
does not pass all three parts, omit it. Transcript and Episodes preserve past
events; memories preserve durable facts; Goals preserve scheduled work.

Compare the preceding committed Turn with the existing slots:

- Add a slot only for a newly established current fact.
- Delete a slot when its state ended, was contradicted, or is no longer useful
  as present context.
- Replace an affected slot by deleting it and adding a freshly written current
  projection. The old value is evidence to reassess, not text to extend or a
  template to copy.
- Return empty add and delete arrays when the Turn only repeats, confirms, or
  reacts to state already represented accurately.

Keep at most one slot for the same live situation. This is deduplication, not a
request to merge its history. A replacement must become shorter as settled or
completed details fall away. Carry forward only the minimum facts that remain
active and useful now; never preserve a detail merely because it appeared in
the old slot.

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

Example: after lunch, travel preparation, and arrival are complete, retain
"Owner is at the station awaiting the 15:20 train", not a recap of lunch,
packing, the journey, and arrival. If the next Turn merely says they are still
waiting, make no change. Once the train departs, replace or delete that slot
according to what remains currently useful.

Typical flow:
… → current_state_finish
