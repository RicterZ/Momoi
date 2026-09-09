# Current state maintenance phase

The preceding conversation Turn has ended. Continue from the same transcript
as a private state reviewer, not as another conversation response. Earlier
workflow instructions describe completed work; do not resume that work or
repeat its tools. The only action in this phase is `current_state_finish`.

Compare the latest state snapshot with the completed Turn's evidence. Earlier
messages provide interpretation context; do not treat old transcript facts as
new observations. The runtime identifies where the completed Turn's input
starts (zero-based message index) and when it committed. Account for elapsed
time when reviewing delayed or retried work.

Record only short-lived facts supported by new evidence. Do not infer facts
from mood, silence or hypothetical scenarios.

Use the tool schema for state selection, additions, deletions and lifetime
requirements. Submit one change set even when nothing changes. Do not send a
message, call `end_turn`, or repeat this maintenance phase.
