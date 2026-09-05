"""Voice delivery contract for configured TTS and supported channels."""

SEND_VOICE_TOOL_SPEC = {
    "name": "send_voice",
    "description": "Speak one complete passage of text as a voice message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}
