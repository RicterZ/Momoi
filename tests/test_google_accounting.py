import asyncio
import unittest
from datetime import datetime, timezone

from momoi.integrations.adapters.deepseek import DeepSeekAccounting
from momoi.integrations.adapters.google import GeminiAccounting, GoogleAIStudioBalanceProvider
from momoi.integrations.registry import adapter_definition


def stamp(year, month, day):
    return datetime(year, month, day, tzinfo=timezone.utc).timestamp()


class GeminiAccountingTests(unittest.TestCase):
    def test_gemini_36_flash_prices_double_from_2027(self):
        accounting = GeminiAccounting(usd_cny_rate=7.0)
        self.assertEqual(
            [round(rate, 6) for rate in accounting.token_rates("gemini-3.6-flash", stamp(2026, 12, 31))],
            [0.525, 5.25, 26.25],
        )
        self.assertEqual(
            [round(rate, 6) for rate in accounting.token_rates("models/gemini-3.6-flash", stamp(2027, 1, 1))],
            [1.05, 10.5, 52.5],
        )

    def test_other_models_keep_deepseek_estimate(self):
        when = stamp(2026, 10, 29)
        self.assertEqual(
            GeminiAccounting().token_rates("deepseek-v4-pro", when),
            DeepSeekAccounting().token_rates("deepseek-v4-pro", when),
        )

    def test_thinking_tokens_are_billed_as_output(self):
        usage = GeminiAccounting().parse_usage({"usage": {
            "completion_tokens": 56, "prompt_tokens": 9198, "total_tokens": 9520,
            "prompt_tokens_details": {"cached_tokens": 9000},
        }})
        self.assertEqual((usage["cache_read"], usage["uncached"], usage["output"]), (9000, 198, 322))
        cost = GeminiAccounting(usd_cny_rate=7.0).estimate_cost(
            "gemini-3.6-flash", stamp(2026, 10, 29), cache_read=9000, uncached=198, output=322)
        self.assertAlmostEqual(cost, (9000 * 0.525 + 198 * 5.25 + 322 * 26.25) / 1_000_000)


class GoogleBalanceAdapterTests(unittest.TestCase):
    def test_registered_balance_adapter_validates_options(self):
        registered = adapter_definition("google", "balance")
        registered.validate({"usd_cny_rate": 7.2})
        with self.assertRaises(Exception):
            registered.validate({"usd_cny_rate": 0})
        with self.assertRaises(Exception):
            registered.validate({"api_key": "x"})

    def test_balance_reports_disabled_and_accounting_toggle(self):
        provider = GoogleAIStudioBalanceProvider()
        self.assertIsInstance(provider.accounting, GeminiAccounting)
        self.assertEqual(asyncio.run(provider.balance())["source"], "disabled")
        self.assertIsNone(GoogleAIStudioBalanceProvider(accounting=False).accounting)
