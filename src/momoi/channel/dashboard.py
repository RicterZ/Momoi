"""Dashboard delivery shares the runtime's durable event and outbox history."""
import secrets


class DashboardChannel:
    name = "dashboard"
    quiet_seconds = 0.5
    max_batch_seconds = 3

    def __init__(self):
        self.typing = False

    async def run(self, on_event, stop):
        await stop.wait()

    async def send_message(self, payload):
        # OutboxWorker records the delivery; the authenticated UI reads that history.
        return f"dashboard:{secrets.token_hex(12)}"

    def content_blocks(self, segments):
        return []

    def workflow_variables(self):
        return {"owner_id": "owner", "dashboard_owner_id": "owner"}

    async def convert_voice(self, voice):
        return voice.native_text or None

    async def set_typing(self, typing):
        self.typing = typing
