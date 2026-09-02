import asyncio
import io
import logging
import unittest
from zoneinfo import ZoneInfo

from momoi.observability.context import current_log_context, log_context
from momoi.observability.events import log_event
from momoi.observability.formatting import KeyValueFormatter
from momoi.observability.values import safe_preview


class LoggingContextTest(unittest.TestCase):
    def test_nested_context_restores_and_formatter_emits_key_values(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("momoi.tests.logging")
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(KeyValueFormatter(ZoneInfo("UTC")))
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
                "X-API-Key": "also-secret",
                "token": "token-secret",
                "url": "https://user:pass@example.com/path?token=secret&q=visible",
                "max_tokens": 8192,
                "image": "data:image/png;base64,abcdef",
                "text": "a\nb",
            }
        )
        self.assertIn('"api_key":"[redacted]"', rendered)
        self.assertIn('"X-API-Key":"[redacted]"', rendered)
        self.assertIn('"token":"[redacted]"', rendered)
        self.assertIn("[redacted]@example.com", rendered)
        self.assertIn("token=%5Bredacted%5D", rendered)
        self.assertIn("q=visible", rendered)
        self.assertIn('"max_tokens":8192', rendered)
        self.assertIn("[omitted 6 base64 chars]", rendered)
        self.assertIn(r"a\nb", rendered)

    def test_formatter_colors_levels_only_when_enabled(self) -> None:
        record = logging.LogRecord(
            "momoi.tests.logging",
            logging.WARNING,
            "",
            0,
            "warning",
            (),
            None,
        )
        plain = KeyValueFormatter(ZoneInfo("UTC")).format(record)
        colored = KeyValueFormatter(ZoneInfo("UTC"), color=True).format(record)
        self.assertNotIn("\033[", plain)
        self.assertTrue(colored.startswith("\033[33m"))
        self.assertTrue(colored.endswith("\033[0m"))


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
