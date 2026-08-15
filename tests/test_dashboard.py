import json
import re
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from momoi.dashboard import (
    JWT_TTL_SECONDS,
    create_dashboard_app,
    issue_dashboard_jwt,
    verify_dashboard_jwt,
)
from momoi.storage import Store


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "momoi.sqlite3", self.root)
        self.secret = "dashboard-secret"
        self.access_token = issue_dashboard_jwt(self.secret)
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
            self.store._db.execute(
                """INSERT INTO reminders
                   (id, text, source_event_id, status, fire_at, schedule_json,
                    created_at, updated_at)
                   VALUES ('reminder-one', '站起来活动一下', 'event', 'pending',
                           ?, '', ?, ?)""",
                (now + 1800, now, now),
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
            TestServer(create_dashboard_app(self.store, token=self.secret))
        )
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.store.close()
        self.temporary.cleanup()

    def _auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.access_token}"}

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
        auth = self._auth()
        overview = await (await self.client.get("/api/overview", headers=auth)).json()
        self.assertEqual(overview["counts"]["conversations"], 1)
        self.assertEqual(overview["counts"]["messages"], 2)
        self.assertEqual(overview["counts"]["reflections"], 1)
        self.assertEqual(overview["counts"]["emotions"], 1)
        self.assertEqual(overview["counts"]["memories"], 3)

        conversations = await (
            await self.client.get("/api/conversations", headers=auth)
        ).json()
        self.assertEqual(conversations["items"][0]["title"], "一次测试聊天")
        conversation = await (
            await self.client.get("/api/conversations/episode-one", headers=auth)
        ).json()
        self.assertEqual(
            [message["content"] for message in conversation["messages"]],
            ["你好，Momoi", "早上好。"],
        )

        memories = await (await self.client.get("/api/memories", headers=auth)).json()
        self.assertEqual(
            [item["activation"] for item in memories["items"]],
            ["always", "recent", "recall"],
        )
        self.assertEqual(memories["items"][0]["content"], "主人不吃香菜。")
        self.assertEqual(memories["items"][1]["evidence"], "我今天在公司")

        reflections = await (
            await self.client.get("/api/reflections", headers=auth)
        ).json()
        self.assertEqual(reflections["items"][0]["summary"], "今天完成了测试。")
        self.assertEqual(reflections["items"][0]["memories"][0]["key"], "testing")

        reminders = await (
            await self.client.get("/api/reminders", headers=auth)
        ).json()
        self.assertEqual(reminders["items"][0]["text"], "站起来活动一下")

        emotions = await (await self.client.get("/api/emotions", headers=auth)).json()
        self.assertNotIn("path", emotions["items"][0])
        asset = await self.client.get(emotions["items"][0]["asset_url"])
        self.assertEqual(asset.status, 200)
        self.assertEqual(await asset.read(), b"GIF89a")

    async def test_dashboard_rejects_invalid_limits(self) -> None:
        response = await self.client.get(
            "/api/conversations?limit=nope", headers=self._auth()
        )
        self.assertEqual(response.status, 400)

    async def test_conversations_are_sorted_by_displayed_update_time(self) -> None:
        self.store.create_episode("较旧的开放聊天", episode_id="older-open")
        self.store.create_episode("较新的已关闭聊天", episode_id="newer-closed")
        with self.store._db:
            self.store._db.execute(
                """UPDATE conversation_episodes
                   SET status='open', updated_at=200 WHERE id='older-open'"""
            )
            self.store._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', updated_at=300 WHERE id='newer-closed'"""
            )
            self.store._db.execute(
                """UPDATE conversation_episodes
                   SET updated_at=100 WHERE id='episode-one'"""
            )

        conversations = await (
            await self.client.get("/api/conversations", headers=self._auth())
        ).json()

        self.assertEqual(
            [item["id"] for item in conversations["items"]],
            ["newer-closed", "older-open", "episode-one"],
        )

    async def test_api_requires_bearer_token_except_emotion_asset(self) -> None:
        memories = await (
            await self.client.get("/api/memories", headers=self._auth())
        ).json()
        memory_id = memories["items"][0]["id"]
        cases = [
            ("GET", "/api/overview", None),
            ("GET", "/api/health", None),
            ("GET", "/api/memories", None),
            ("GET", "/api/emotions", None),
            ("PATCH", f"/api/memories/{memory_id}", {"content": "改掉"}),
            ("DELETE", f"/api/memories/{memory_id}", None),
            (
                "PATCH",
                "/api/goals/goal-one",
                {"title": "被改", "success_criteria": "x", "next_action": "y", "status": "active"},
            ),
            ("DELETE", "/api/goals/goal-one", {"reason": "nope"}),
            ("GET", "/api/reminders", None),
            ("DELETE", "/api/reminders/reminder-one", None),
            ("DELETE", "/api/emotions/hello", None),
        ]
        for method, path, body in cases:
            unauthorized = await self.client.request(method, path, json=body)
            self.assertEqual(unauthorized.status, 401, msg=f"{method} {path}")
            wrong = await self.client.request(
                method, path, json=body, headers=self._auth("wrong")
            )
            self.assertEqual(wrong.status, 401, msg=f"{method} {path} wrong token")
            raw_secret = await self.client.request(
                method, path, json=body, headers=self._auth(self.secret)
            )
            self.assertEqual(
                raw_secret.status, 401, msg=f"{method} {path} raw secret"
            )

        asset = await self.client.get("/api/emotions/hello/asset")
        self.assertEqual(asset.status, 200)
        self.assertEqual(await asset.read(), b"GIF89a")

        still = await (
            await self.client.get("/api/goals?all=true", headers=self._auth())
        ).json()
        self.assertEqual(still["items"][0]["status"], "active")
        emotions = await (
            await self.client.get("/api/emotions", headers=self._auth())
        ).json()
        self.assertEqual(emotions["items"][0]["slug"], "hello")

    async def test_reminder_list_and_cancel(self) -> None:
        auth = self._auth()
        pending = await (
            await self.client.get("/api/reminders", headers=auth)
        ).json()
        self.assertEqual([item["id"] for item in pending["items"]], ["reminder-one"])

        cancelled = await self.client.delete(
            "/api/reminders/reminder-one", headers=auth
        )
        self.assertEqual(cancelled.status, 200)
        self.assertEqual((await cancelled.json())["status"], "cancelled")

        pending = await (
            await self.client.get("/api/reminders", headers=auth)
        ).json()
        self.assertEqual(pending["items"], [])
        all_items = await (
            await self.client.get("/api/reminders?all=true", headers=auth)
        ).json()
        self.assertEqual(all_items["items"][0]["status"], "cancelled")

    async def test_auth_token_issues_year_long_jwt(self) -> None:
        denied = await self.client.post(
            "/api/auth/token", json={"token": "wrong"}
        )
        self.assertEqual(denied.status, 401)

        issued = await self.client.post(
            "/api/auth/token", json={"token": self.secret}
        )
        self.assertEqual(issued.status, 200)
        payload = await issued.json()
        self.assertEqual(payload["token_type"], "Bearer")
        self.assertEqual(payload["expires_in"], JWT_TTL_SECONDS)
        self.assertTrue(verify_dashboard_jwt(payload["token"], self.secret))

        overview = await self.client.get(
            "/api/overview", headers=self._auth(payload["token"])
        )
        self.assertEqual(overview.status, 200)

        expired = issue_dashboard_jwt(
            self.secret, ttl_seconds=1, now=int(time.time()) - 10
        )
        self.assertFalse(verify_dashboard_jwt(expired, self.secret))
        rejected = await self.client.get(
            "/api/overview", headers=self._auth(expired)
        )
        self.assertEqual(rejected.status, 401)

    async def test_empty_dashboard_token_rejects_all_api(self) -> None:
        bare = TestClient(TestServer(create_dashboard_app(self.store, token="")))
        await bare.start_server()
        try:
            login = await bare.post(
                "/api/auth/token", json={"token": "anything"}
            )
            self.assertEqual(login.status, 401)
            for method, path, body in (
                ("GET", "/api/overview", None),
                (
                    "DELETE",
                    "/api/goals/goal-one",
                    {"reason": "nope"},
                ),
            ):
                response = await bare.request(
                    method,
                    path,
                    json=body,
                    headers={"Authorization": "Bearer anything"},
                )
                self.assertEqual(response.status, 401, msg=f"{method} {path}")
            asset = await bare.get("/api/emotions/hello/asset")
            self.assertEqual(asset.status, 200)
        finally:
            await bare.close()

    async def test_memory_update_and_delete(self) -> None:
        auth = self._auth()
        memories = await (await self.client.get("/api/memories", headers=auth)).json()
        memory_id = memories["items"][0]["id"]
        updated = await (
            await self.client.patch(
                f"/api/memories/{memory_id}",
                json={"content": "主人非常不吃香菜。"},
                headers=auth,
            )
        ).json()
        self.assertEqual(updated["content"], "主人非常不吃香菜。")
        deleted = await self.client.delete(
            f"/api/memories/{memory_id}", headers=auth
        )
        self.assertEqual(deleted.status, 200)
        remaining = await (await self.client.get("/api/memories", headers=auth)).json()
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
        listed = await (
            await self.client.get("/api/emotions", headers=self._auth())
        ).json()
        self.assertEqual([row["slug"] for row in listed["items"]], ["hello"])


if __name__ == "__main__":
    unittest.main()
