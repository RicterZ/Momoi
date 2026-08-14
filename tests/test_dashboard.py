import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from momoi.dashboard import create_dashboard_app
from momoi.storage import Store


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "momoi.sqlite3", self.root)
        now = time.time()

        self.store.create_episode("一次测试聊天", episode_id="episode-one")
        with self.store._db:
            self.store._db.execute(
                """INSERT INTO turns
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES ('turn-one', 'owner', '[]', 'completed', ?, ?)""",
                (now, now),
            )
            self.store._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES ('turn-one', 'user', '你好，Momoi', ?, '[]')""",
                (now,),
            )
            self.store._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    delivery_state)
                   VALUES ('turn-one', 'assistant', '早上好。', ?, '[]',
                           'delivered')""",
                (now,),
            )
            self.store._db.execute(
                """INSERT INTO reflections
                   (id, local_date, state, scheduled_at, summary, memories_json,
                    created_at, completed_at)
                   VALUES ('reflection:2026-08-13', '2026-08-13', 'completed',
                           ?, '今天完成了测试。', ?, ?, ?)""",
                (
                    now,
                    json.dumps(
                        [
                            {
                                "kind": "practice",
                                "key": "testing",
                                "content": "保持验证。",
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
        self.store.link_turn_to_episode("episode-one", "turn-one")
        with self.store._db:
            self.store._db.executemany(
                """INSERT INTO memories
                   (kind, key, content, activation, authority, source_event_id,
                    evidence_quote, importance, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'owner', 'event-one', ?, 0.8, ?, ?)""",
                [
                    (
                        "preference",
                        "food.no_cilantro",
                        "主人不吃香菜。",
                        "always",
                        "我不吃香菜",
                        now - 86400,
                        now - 3600,
                    ),
                    (
                        "routine",
                        "location.today",
                        "今天在公司，晚上才回家。",
                        "recent",
                        "我今天在公司",
                        now - 7200,
                        now - 1800,
                    ),
                    (
                        "shared",
                        "game.ba",
                        "一起在补《碧蓝档案》剧情。",
                        "recall",
                        "先把 BA 主线补一下",
                        now - 172800,
                        now - 86400,
                    ),
                ],
            )

        image = self.root / "reaction.gif"
        image.write_bytes(b"GIF89a")
        self.store.add_emotion("hello", image, "打招呼")

        self.client = TestClient(TestServer(create_dashboard_app(self.store)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.store.close()
        self.temporary.cleanup()

    async def test_dashboard_serves_static_ui_with_security_headers(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        page = await response.text()
        self.assertIn("Momoi Dashboard", page)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        script_path = re.search(r'src="(/assets/[^"]+\.js)"', page)
        self.assertIsNotNone(script_path)
        script = await self.client.get(script_path.group(1))
        self.assertEqual(script.status, 200)
        self.assertIn("javascript", script.content_type)

    async def test_dashboard_exposes_read_only_records(self) -> None:
        overview = await (await self.client.get("/api/overview")).json()
        self.assertEqual(overview["counts"]["conversations"], 1)
        self.assertEqual(overview["counts"]["messages"], 2)
        self.assertEqual(overview["counts"]["reflections"], 1)
        self.assertEqual(overview["counts"]["emotions"], 1)
        self.assertEqual(overview["counts"]["memories"], 3)

        conversations = await (
            await self.client.get("/api/conversations")
        ).json()
        self.assertEqual(conversations["items"][0]["title"], "一次测试聊天")
        conversation = await (
            await self.client.get("/api/conversations/episode-one")
        ).json()
        self.assertEqual(
            [message["content"] for message in conversation["messages"]],
            ["你好，Momoi", "早上好。"],
        )

        memories = await (await self.client.get("/api/memories")).json()
        self.assertEqual(
            [item["activation"] for item in memories["items"]],
            ["always", "recent", "recall"],
        )
        self.assertEqual(memories["items"][0]["content"], "主人不吃香菜。")
        self.assertEqual(memories["items"][1]["evidence"], "我今天在公司")

        reflections = await (await self.client.get("/api/reflections")).json()
        self.assertEqual(reflections["items"][0]["summary"], "今天完成了测试。")
        self.assertEqual(reflections["items"][0]["memories"][0]["key"], "testing")

        emotions = await (await self.client.get("/api/emotions")).json()
        self.assertNotIn("path", emotions["items"][0])
        asset = await self.client.get(emotions["items"][0]["asset_url"])
        self.assertEqual(asset.status, 200)
        self.assertEqual(await asset.read(), b"GIF89a")

    async def test_dashboard_rejects_invalid_limits(self) -> None:
        response = await self.client.get("/api/conversations?limit=nope")
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
