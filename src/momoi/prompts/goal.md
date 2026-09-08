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
- Use `send_bubbles` or available `send_voice` for a due notification, useful
  result, needed decision, or meaningful failure. These calls start delivery
  immediately, independently of `end_turn`. Avoid duplicate or obsolete
  information; avoid assuming unknown circumstances.
- Tools and messages may alternate. When nothing needs sending, still submit the
  Goal outcome through `end_turn`; assistant text does not end this Turn.
- After work, call `end_turn` with only `goal`; it may follow delivery in the
  same response.
  Every outcome requires status and a concrete result. The runtime supplies the
  current Goal ID; omit goal_id, mood, reply_wait, activity, and heartbeat.
  - done: success criteria satisfied. cancelled: no longer pursued. Both accept
    only status and result; use result to explain the verified outcome or cancellation.
  - active: include next_action and a future next_review_at, or omit next_review_at
    to reuse an existing recurring schedule. A recurring active Goal must omit
    next_review_at; clear_schedule removes recurrence when changing to a one-off review.
  - waiting: include waiting_for and a future next_review_at, even if recurring.
  - blocked: include blocked_reason and omit next_review_at.
- Review timestamps use ISO 8601 with a timezone. Do not call goal_update,
  goal_finish, or goal_cancel during this review. `end_turn` records the outcome
  and ends the Turn; it does not send a message.

Completed review arguments for `end_turn`:
`{"goal":{"status":"done","result":"File downloaded and verified"}}`.
