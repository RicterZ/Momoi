"""Voice delivery contract for configured TTS and supported channels."""

SEND_VOICE_TOOL_SPEC = {
    "name": "send_voice",
    "description": (
        "Speak one complete passage of text as a voice message. "
        "Voice messages must contain only words to be spoken: no stickers, "
        "reaction images, or emotion:// directives."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The complete passage to speak aloud. Do not include stickers, "
                    "reaction images, or emotion:// directives."
                ),
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}
