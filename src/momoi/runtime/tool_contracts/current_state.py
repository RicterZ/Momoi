"""Stable visible schema; the harness grants execution only during maintenance."""

from ...storage.current_state import CurrentStateManager


def current_state_finish_spec() -> dict:
    return {
        "name": "current_state_finish",
        "description": (
            "Commit the current-state change set and finish the private maintenance "
            "phase after a conversation Turn has committed. Only callable in "
            "current_state_maintenance; unavailable during the conversation itself."
        ),
        "input_schema": CurrentStateManager.change_schema(),
    }
