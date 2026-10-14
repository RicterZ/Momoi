import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from momoi.integrations.adapters.deepseek import DeepSeekAccounting
from momoi.llm.usage import summarize_usage


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _plugin() -> DeepSeekAccounting:
    return DeepSeekAccounting()




class DeepSeekAccountingTest(unittest.TestCase):
    def test_current_rates_apply_to_all_models_and_recorded_dates(self) -> None:
        plugin = _plugin()
        weekday_peak = datetime(2026, 8, 17, 10, 0, tzinfo=SHANGHAI).timestamp()
        weekday_offpeak = datetime(2026, 8, 17, 21, 0, tzinfo=SHANGHAI).timestamp()
        for model in ("deepseek-v4-flash", "deepseek-flash"):
            self.assertEqual(plugin.token_rates(model, weekday_peak), (0.04, 2.0, 8.0))
            self.assertEqual(plugin.token_rates(model, weekday_offpeak), (0.02, 1.0, 4.0))
        self.assertAlmostEqual(
            plugin.estimate_cost(
                "deepseek-v4-flash",
                weekday_peak,
                cache_read=500_000,
                uncached=500_000,
                output=100_000,
            ),
            1.82,
        )

    def test_pro_rates_and_unknown_alias_estimate(self):
        plugin = _plugin()
        peak = datetime(2026, 10, 13, 10, tzinfo=SHANGHAI).timestamp()
        self.assertEqual(plugin.token_rates("deepseek-v4-pro", peak), (0.30, 9.0, 27.0))
        self.assertEqual(plugin.token_rates("custom-model", peak), (0.04, 2.0, 8.0))
        self.assertEqual(plugin.token_rates("custom-model", peak + 12 * 3600), (0.04, 2.0, 8.0))

    def test_weekday_peak_boundaries_use_beijing_time(self) -> None:
        plugin = _plugin()
        expected = {
            (8, 59): (0.02, 1.0, 4.0),
            (9, 0): (0.04, 2.0, 8.0),
            (11, 59): (0.04, 2.0, 8.0),
            (12, 0): (0.02, 1.0, 4.0),
            (13, 59): (0.02, 1.0, 4.0),
            (14, 0): (0.04, 2.0, 8.0),
            (17, 59): (0.04, 2.0, 8.0),
            (18, 0): (0.02, 1.0, 4.0),
        }
        for (hour, minute), rates in expected.items():
            timestamp = datetime(2026, 9, 11, hour, minute, tzinfo=SHANGHAI).timestamp()
            self.assertEqual(plugin.token_rates("deepseek-v4-flash", timestamp), rates)

    def test_weekends_are_offpeak_even_during_peak_hours(self) -> None:
        plugin = _plugin()
        for day in (12, 13):
            for hour in (9, 10, 14, 17):
                timestamp = datetime(2026, 9, day, hour, tzinfo=SHANGHAI).timestamp()
                self.assertEqual(plugin.token_rates("deepseek-v4-pro", timestamp), (0.15, 4.5, 13.5))

    def test_parse_usage_reads_deepseek_billing_fields(self) -> None:
        plugin = _plugin()
        self.assertEqual(
            plugin.parse_usage(
                {
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 20,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                        "completion_tokens_details": {"reasoning_tokens": 80},
                    }
                }
            ),
            {
                "input": 1000,
                "uncached": 200,
                "cache_read": 800,
                "cache_write": 0,
                "output": 100,
                "total": 1100,
                "cache_hit_rate": 80.0,
                "cache_reported": True,
            },
        )
        masked = plugin.parse_usage(
            {
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
            }
        )
        self.assertEqual(masked["cache_read"], 800)
        self.assertEqual(masked["uncached"], 200)
        self.assertEqual(
            plugin.parse_usage(
                {
                    "usage": {
                        "prompt_tokens": 1000,
                        "output_tokens": 50,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                    }
                }
            )["output"],
            50,
        )
        self.assertEqual(
            plugin.parse_usage(
                {
                    "usage": {
                        "prompt_tokens": 58,
                        "completion_tokens": 92,
                        "completion_tokens_details": {"reasoning_tokens": 85},
                    }
                }
            )["output"],
            92,
        )
        self.assertEqual(
            plugin.parse_usage(
                {
                    "usage": {
                        "prompt_tokens": 1000,
                        "output_tokens": 200,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                        "completion_tokens_details": {"reasoning_tokens": 80},
                    }
                }
            )["output"],
            280,
        )
        self.assertEqual(
            plugin.parse_usage(
                {
                    "usage": {
                        "prompt_tokens": 1000,
                        "output_tokens": 200,
                        "total_tokens": 1280,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                        "completion_tokens_details": {"reasoning_tokens": 80},
                    }
                }
            )["output"],
            280,
        )
        self.assertEqual(
            plugin.parse_usage(
                {
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 800,
                        "completion_tokens_details": {"reasoning_tokens": 40},
                    }
                }
            )["output"],
            90,
        )

    def test_summarize_usage_uses_plugin_rates(self) -> None:
        now = datetime(2026, 8, 15, 20, 0, tzinfo=SHANGHAI).timestamp()
        earlier = now - 86400
        summary = summarize_usage(
            [
                {
                    "created_at": earlier,
                    "model": "deepseek-v4-flash",
                    "stage": "heartbeat",
                    "input_tokens": 1000,
                    "uncached_tokens": 200,
                    "cache_read_tokens": 800,
                    "cache_write_tokens": 0,
                    "output_tokens": 100,
                    "cache_reported": 1,
                },
                {
                    "created_at": now,
                    "model": "deepseek-v4-pro",
                    "stage": "webhook",
                    "input_tokens": 100,
                    "uncached_tokens": 100,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 20,
                    "cache_reported": 1,
                },
            ],
            days=2,
            now=now,
            zone=SHANGHAI,
            estimate=_plugin().estimate_cost,
        )
        self.assertEqual(summary["totals"]["requests"], 2)
        self.assertEqual(summary["today"]["requests"], 1)
        self.assertGreater(summary["totals"]["estimated_cost"], 0)

    def test_summarize_usage_without_pricing_keeps_tokens_and_omits_cost(self) -> None:
        now = datetime(2026, 8, 15, 20, 0, tzinfo=SHANGHAI).timestamp()
        summary = summarize_usage(
            [
                {
                    "created_at": now,
                    "model": "deepseek-v4-flash",
                    "stage": "owner",
                    "input_tokens": 120,
                    "uncached_tokens": 20,
                    "cache_read_tokens": 100,
                    "cache_write_tokens": 0,
                    "output_tokens": 15,
                    "cache_reported": 1,
                }
            ],
            days=1,
            now=now,
            zone=SHANGHAI,
        )
        self.assertFalse(summary["cost_available"])
        self.assertEqual(summary["today"]["input_tokens"], 120)
        self.assertEqual(summary["today"]["output_tokens"], 15)
        self.assertIsNone(summary["today"]["estimated_cost"])
        self.assertIsNone(summary["daily"][0]["estimated_cost"])


class StoreUsageTest(unittest.TestCase):
    def test_hourly_usage_matches_daily_totals_and_local_day_boundaries(self) -> None:
        import tempfile
        from pathlib import Path
        from momoi.storage import Store

        with tempfile.TemporaryDirectory() as raw:
            store = Store(Path(raw) / "momoi.sqlite3", timezone="Asia/Shanghai")
            store.set_usage_accounting(_plugin())
            start = datetime(2026, 9, 5, tzinfo=SHANGHAI)
            for offset in (-1, 0, 3599, 3600, 86399, 86400):
                store.record_llm_call(
                    created_at=(start + timedelta(seconds=offset)).timestamp(),
                    turn_id=f"hour-{offset}", stage="owner", model="deepseek-v4-flash",
                    metrics={"input": 200, "uncached": 50, "cache_read": 150,
                             "output": 30, "cache_reported": True},
                )
            hourly = store.dashboard_hourly_usage("2026-09-05")
            daily = store.dashboard_usage(days=1, now=(start + timedelta(hours=12)).timestamp())
            self.assertEqual(hourly["totals"], daily["totals"])
            self.assertEqual(hourly["totals"]["requests"], 4)
            self.assertEqual(hourly["timezone"], "Asia/Shanghai")
            self.assertEqual(hourly["hourly"][0]["requests"], 2)
            self.assertEqual(hourly["hourly"][1]["requests"], 1)
            self.assertEqual(hourly["hourly"][23]["requests"], 1)
            self.assertEqual(hourly["hourly"][2]["requests"], 0)
            self.assertEqual(hourly["totals"]["cache_hit_rate"], 75.0)
            empty = store.dashboard_hourly_usage("2026-09-03")
            self.assertEqual(len(empty["hourly"]), 24)
            self.assertEqual(empty["totals"]["requests"], 0)
            store.close()

    def test_hourly_usage_keeps_24_clock_hours_across_dst(self) -> None:
        import tempfile
        from pathlib import Path
        from momoi.storage import Store

        zone = ZoneInfo("America/New_York")
        with tempfile.TemporaryDirectory() as raw:
            store = Store(Path(raw) / "momoi.sqlite3", timezone=zone.key)
            moments = [
                datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0),
                datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1),
                datetime(2026, 11, 1, 23, 59, tzinfo=zone),
                datetime(2026, 11, 2, 0, 0, tzinfo=zone),
            ]
            for index, moment in enumerate(moments):
                store.record_llm_call(
                    created_at=moment.timestamp(), turn_id=f"dst-{index}",
                    stage="owner", model="test", metrics={"input": 10},
                )
            hourly = store.dashboard_hourly_usage("2026-11-01")
            self.assertEqual(len(hourly["hourly"]), 24)
            self.assertEqual(hourly["hourly"][1]["requests"], 2)
            self.assertEqual(hourly["totals"]["requests"], 3)
            self.assertEqual(len(store.dashboard_hourly_usage("2026-03-08")["hourly"]), 24)
            store.close()

    def test_record_llm_call_feeds_dashboard(self) -> None:
        import tempfile
        from pathlib import Path

        from momoi.storage import Store

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = Store(root / "momoi.sqlite3", root)
            store.set_usage_accounting(_plugin())
            now = time.time()
            store.record_llm_call(
                created_at=now,
                turn_id="turn-one",
                stage="owner",
                model="deepseek-v4-flash",
                metrics={
                    "input": 200,
                    "uncached": 50,
                    "cache_read": 150,
                    "cache_write": 0,
                    "output": 30,
                    "cache_reported": True,
                },
            )
            usage = store.dashboard_usage(days=1, now=now)
            self.assertEqual(usage["today"]["requests"], 1)
            self.assertEqual(usage["today"]["cache_read_tokens"], 150)
            self.assertTrue(usage["cost_available"])
            self.assertEqual(store.dashboard_overview()["usage"]["today"]["requests"], 1)
            store.close()


def test_deepseek_responses_cached_usage():
    usage = DeepSeekAccounting().parse_usage({"usage": {
        "input_tokens": 4188, "input_tokens_details": {"cached_tokens": 3456},
        "output_tokens": 297, "output_tokens_details": {"reasoning_tokens": 211},
        "total_tokens": 4485,
    }})
    assert usage["input"] == 4188
    assert usage["cache_read"] == 3456
    assert usage["uncached"] == 732
    assert usage["output"] == 297  # Reasoning is already included, not added twice.
    assert usage["total"] == 4485
    assert usage["cache_reported"] is True


def test_deepseek_responses_explicit_zero_cache():
    usage = DeepSeekAccounting().parse_usage({"usage": {
        "input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 2,
    }})
    assert usage["cache_reported"] is True
    assert usage["uncached"] == 100
    assert usage["cache_read"] == 0
