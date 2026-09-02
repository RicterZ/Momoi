import json
import tempfile
import unittest
from pathlib import Path

from momoi.storage import Store


class ContextPlanAdapterTest(unittest.TestCase):
    def test_legacy_record_is_normalized_at_storage_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("legacy", "owner", ["legacy-event"])
            legacy_plan = {
                "intent_units": [
                    {
                        "id": "device",
                        "recall_queries": ["设备名称 | device-42"],
                    }
                ]
            }
            with store._db:
                store._db.execute(
                    """INSERT INTO context_plans
                       (turn_id, revision, source_event_ids_json, plan_json,
                        retrieval_json, state, created_at, updated_at)
                       VALUES ('legacy', 1, '["legacy-event"]', ?, ?,
                               'recalled', 1, 1)""",
                    (
                        json.dumps(legacy_plan, ensure_ascii=False),
                        json.dumps(
                            {
                                "version": 5,
                                "query_recall": (
                                    "queries=设备名称 | device-42\n"
                                    "hits=设备名称 | device-42"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            record = store.context_plan("legacy")

            self.assertEqual(record["retrieval"]["version"], 6)
            self.assertEqual(
                record["retrieval"]["effective_recall_queries"],
                ["设备名称 | device-42"],
            )
            query = record["plan"]["intent_units"][0]["recall_queries"][0]
            self.assertEqual(
                query,
                {
                    "semantic": "设备名称 | device-42",
                    "keywords": ["设备名称", "device-42"],
                },
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
