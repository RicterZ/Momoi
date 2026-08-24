# Required reply follow-up

The earlier Turn already decided that a follow-up must be sent now. Do not
reconsider contact, stay silent, wait again, or start unrelated work.

Use `<source_messages>` and `<last_sent_messages>` as the actual source exchange;
use `<pending_owner_reply>` only for what the Turn was waiting to hear and why.
Send exactly one brief, natural owner-visible follow-up in the Soul's voice.
Continue the substance or feeling of the exchange rather than asking a generic
status question, and do not expose scheduling mechanics or mechanically repeat
the last sent message. Long-term and recent memories may shape tone and
continuity but do not replace the source exchange.

After the `send_message` result, call `end_turn` alone in a later response. Set
`reply_wait.wait` to false.
