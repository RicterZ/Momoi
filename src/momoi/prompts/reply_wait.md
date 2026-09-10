# Required reply follow-up

Carry out `<followup>` now. The earlier Turn ended, but its conversational beat
remains open. This trigger is not a new owner message.

Typical flow:
reply_followup → send_bubbles? → … → end_turn

- Continue from the last delivered bubble using the stated reason and elapsed
  silence. Do not answer old messages again, repeat sent words, or assume why
  the owner has been silent.
- Contact is already due. Do not reconsider it or schedule another wait. You may
  complete work relevant to this conversational beat before ending the Turn.
