# Due Goal contract

Continue `<due_goal>` within its recorded authority. This trigger is not owner
speech; the transcript informs this Goal, not unrelated work.

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
- After work, submit the Goal outcome through `goal_review`, including when nothing
  needs sending. After it succeeds, call `end_turn` with `{}` to commit and finish.
  A completed occurrence does not close an ongoing recurring Goal.
