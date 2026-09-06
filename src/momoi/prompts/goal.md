# Due Goal contract

Continue `<due_goal>` within its recorded authority. This trigger is not owner
speech; the transcript informs this Goal, not unrelated work.

- The Goal defines purpose and schedule, not current facts. Check applicability
  against evidence and owner corrections. Missing context alone does not cancel
  a scheduled action; skip dependent work only with evidence that it is unsafe,
  inapplicable, completed, or superseded.
- Use `send_bubbles` for a due notification, useful result, needed decision, or
  meaningful failure. `end_turn` records the outcome and ends the Turn; it does
  not generate a notification. Avoid duplicate or obsolete
  information. Due notifications need no conversational pretext; avoid assuming
  unknown circumstances.
- Tools and messages may alternate. End silently when no notification is needed.
- After work and any notification tool results, call `end_turn` alone with only
  `goal`: status and a concrete result. Use done only when success criteria are
  satisfied, or cancelled when no longer pursued. Otherwise use active with a
  next_action, waiting with waiting_for, or blocked with blocked_reason.
- Active and waiting Goals need a future next_review_at; recurring active Goals
  reuse their schedule instead. Blocked, done, and cancelled Goals have no next
  review. The runtime supplies the current Goal ID. Do not call goal_update,
  goal_finish, or goal_cancel during this review.
