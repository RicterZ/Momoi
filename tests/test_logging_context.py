import asyncio
import io
import logging
import unittest

from momoi.logging_context import (
    KeyValueFormatter,
    current_log_context,
    log_context,
    log_event,
    safe_preview,
)


class LoggingContextTest(unittest.TestCase):
    def test_nested_context_restores_and_formatter_emits_key_values(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("momoi.tests.logging")
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(KeyValueFormatter())
        logger.addHandler(handler)

        with log_context(stage="owner", turn_id="turn-1"):
            with log_context(call_id="call-1", round=2):
                log_event(
                    logger,
                    logging.DEBUG,
                    "llm_request",
                    model="test model",
                    messages=1,
                )
            self.assertEqual(
                current_log_context(), {"stage": "owner", "turn_id": "turn-1"}
            )
        self.assertEqual(current_log_context(), {})
        rendered = stream.getvalue()
        self.assertIn("event=llm_request", rendered)
        self.assertIn("stage=owner", rendered)
        self.assertIn("turn_id=turn-1", rendered)
        self.assertIn("call_id=call-1", rendered)
        self.assertIn('model="test model"', rendered)

    def test_safe_preview_redacts_secrets_media_and_newlines(self) -> None:
        rendered = safe_preview(
            {
                "api_key": "secret",
                "max_tokens": 8192,
                "image": "data:image/png;base64,abcdef",
                "text": "a\nb",
            }
        )
        self.assertIn('"api_key":"[redacted]"', rendered)
        self.assertIn('"max_tokens":8192', rendered)
        self.assertIn("[omitted 6 base64 chars]", rendered)
        self.assertIn(r"a\nb", rendered)


class LoggingContextAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_contexts_do_not_leak(self) -> None:
        async def observe(turn_id: str) -> tuple[str, str]:
            with log_context(stage="owner", turn_id=turn_id):
                before = str(current_log_context()["turn_id"])
                await asyncio.sleep(0)
                after = str(current_log_context()["turn_id"])
                return before, after

        first, second = await asyncio.gather(observe("turn-a"), observe("turn-b"))
        self.assertEqual(first, ("turn-a", "turn-a"))
        self.assertEqual(second, ("turn-b", "turn-b"))
        self.assertEqual(current_log_context(), {})
