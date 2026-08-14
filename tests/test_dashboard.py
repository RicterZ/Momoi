import json
import re
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from momoi.dashboard import create_dashboard_app
from momoi.storage import Store


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "momoi.sqlite3", self.root)
        self.token = "dashboard-secret"
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
            self.store._db.execute(
                """INSERT INTO goals
                   (id, title, success_criteria, authority, source_event_id, status,
                    plan_json, next_action, waiting_for, blocked_reason, latest_result,
                    schedule_json, next_review_at, created_at, updated_at)
                   VALUES ('goal-one', '整理桌面', '桌面干净', 'owner', 'event',
                           'active', '[]', '收拾文件', '', '', '', '', ?, ?, ?)""",
                (now + 3600, now, now),
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

        self.client = TestClient(
            TestServer(create_dashboard_app(self.store, token=self.token))
        )
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.store.close()
        self.temporary.cleanup()

    def _auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

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

    async def test_writes_require_bearer_token(self) -> None:
        memories = await (await self.client.get("/api/memories")).json()
        memory_id = memories["items"][0]["id"]
        cases = [
            ("PATCH", f"/api/memories/{memory_id}", {"content": "改掉"}),
            ("DELETE", f"/api/memories/{memory_id}", None),
            (
                "PATCH",
                "/api/goals/goal-one",
                {"title": "被改", "success_criteria": "x", "next_action": "y", "status": "active"},
            ),
            ("DELETE", "/api/goals/goal-one", {"reason": "nope"}),
            ("DELETE", "/api/emotions/hello", None),
        ]
        for method, path, body in cases:
            unauthorized = await self.client.request(method, path, json=body)
            self.assertEqual(unauthorized.status, 401, msg=f"{method} {path}")
            wrong = await self.client.request(
                method, path, json=body, headers=self._auth("wrong")
            )
            self.assertEqual(wrong.status, 401, msg=f"{method} {path} wrong token")

        still = await (await self.client.get("/api/goals?all=true")).json()
        self.assertEqual(still["items"][0]["status"], "active")
        emotions = await (await self.client.get("/api/emotions")).json()
        self.assertEqual(emotions["items"][0]["slug"], "hello")

    async def test_empty_dashboard_token_rejects_all_writes(self) -> None:
        bare = TestClient(TestServer(create_dashboard_app(self.store, token="")))
        await bare.start_server()
        try:
            response = await bare.delete(
                "/api/goals/goal-one",
                json={"reason": "nope"},
                headers={"Authorization": "Bearer anything"},
            )
            self.assertEqual(response.status, 401)
        finally:
            await bare.close()

    async def test_memory_update_and_delete(self) -> None:
        memories = await (await self.client.get("/api/memories")).json()
        memory_id = memories["items"][0]["id"]
        updated = await (
            await self.client.patch(
                f"/api/memories/{memory_id}",
                json={"content": "主人非常不吃香菜。"},
                headers=self._auth(),
            )
        ).json()
        self.assertEqual(updated["content"], "主人非常不吃香菜。")
        deleted = await self.client.delete(
            f"/api/memories/{memory_id}", headers=self._auth()
        )
        self.assertEqual(deleted.status, 200)
        remaining = await (await self.client.get("/api/memories")).json()
        self.assertEqual(len(remaining["items"]), 2)

    async def test_goal_update_and_cancel(self) -> None:
        updated = await (
            await self.client.patch(
                "/api/goals/goal-one",
                json={
                    "title": "整理房间",
                    "success_criteria": "房间干净",
                    "next_action": "先收拾桌面",
                    "status": "active",
                },
                headers=self._auth(),
            )
        ).json()
        self.assertEqual(updated["title"], "整理房间")
        self.assertEqual(updated["next_action"], "先收拾桌面")
        cancelled = await (
            await self.client.delete(
                "/api/goals/goal-one",
                json={"reason": "先不做了"},
                headers=self._auth(),
            )
        ).json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["latest_result"], "先不做了")

    async def test_emotion_create_update_and_delete(self) -> None:
        form = FormData()
        form.add_field("slug", "wave")
        form.add_field("description", "挥手")
        form.add_field(
            "file",
            BytesIO(b"GIF89a\x01\x02"),
            filename="wave.gif",
            content_type="image/gif",
        )
        created = await self.client.post(
            "/api/emotions", data=form, headers=self._auth()
        )
        self.assertEqual(created.status, 201)
        item = await created.json()
        self.assertEqual(item["slug"], "wave")
        self.assertEqual(item["description"], "挥手")
        self.assertNotIn("path", item)

        patched = await (
            await self.client.patch(
                "/api/emotions/wave",
                json={"description": "热情挥手"},
                headers=self._auth(),
            )
        ).json()
        self.assertEqual(patched["description"], "热情挥手")

        deleted = await self.client.delete(
            "/api/emotions/wave", headers=self._auth()
        )
        self.assertEqual(deleted.status, 200)
        listed = await (await self.client.get("/api/emotions")).json()
        self.assertEqual([row["slug"] for row in listed["items"]], ["hello"])


if __name__ == "__main__":
    unittest.main()
