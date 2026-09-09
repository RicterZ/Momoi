# Due Goal contract

Continue `<due_goal>` within its recorded authority. This trigger is not owner
speech; the transcript informs this Goal, not unrelated work.

Typical flow:
… → send_bubbles? / send_voice? → … → goal_review → end_turn

Your workflow may involve multiple activities and tool calls. You may call
multiple independent tools in one response; wait for results before making
dependent calls.

- `<due_goal>` is the current task and state. `<recent_goals>` lists all historical
  `<goal>` review IDs in the current transcript, in timeline order. Read their
  results and surrounding messages there; they are not new tasks or proof of
  delivery. Retrieve older evidence only for a specific unresolved need.
- The Goal defines purpose and schedule, not current facts. Check applicability
  against evidence and owner corrections. Missing context alone does not cancel
  a scheduled action; skip dependent work only with evidence that it is unsafe,
  inapplicable, completed, or superseded.
- Notify for a due reminder, useful result, needed decision, or meaningful
  failure. Avoid duplicate or obsolete information and assumptions about unknown
  circumstances.
- `goal_review` must succeed before `end_turn`, even with nothing to send.
  A completed occurrence does not close an ongoing recurring Goal.
