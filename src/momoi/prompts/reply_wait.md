# Required reply follow-up

Carry out `<followup>` now. The earlier Turn ended, but its conversational beat
remains open. This trigger is not a new owner message.

Typical flow:
send_bubbles / send_voice → end_turn

Delivery and `end_turn` may share one response, with `end_turn` last.

- Continue from the last delivered bubble using the stated reason and elapsed
  silence. Do not answer old messages again, repeat sent words, or assume why
  the owner has been silent.
- Contact is already due. Do not reconsider it, schedule another wait, or begin
  unrelated work. Use shared history and memory to make the continuation fit.
