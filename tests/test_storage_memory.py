import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


from momoi.agenda_tools import AgendaTools
from momoi.builtin_tools import BuiltinTools
from momoi.channel.napcat import NapCatConfig
from momoi.config import (
    AppConfig,
    HeartbeatConfig,
    LLMConfig,
    NotificationConfig,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.memory_tools import MemoryTools
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ToolCall,
    TurnDraft,
)
from momoi.storage import Store
from momoi.storage.scheduling import next_schedule_at


class StorageMemoryTest(unittest.TestCase):
    def test_legacy_outbox_migrates_to_typed_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(path)
            database.execute(
                """CREATE TABLE outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    possible_duplicate INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                )"""
            )
            database.execute(
                "INSERT INTO outbox(turn_id, dedupe_key, text) VALUES ('old', 'old:0', '旧消息')"
            )
            database.commit()
            database.close()

            store = Store(path)
            row = store.due_outbox()[0]
            self.assertEqual(row.kind, "text")
            self.assertIsNone(row.media_path)
            self.assertEqual(row.text, "旧消息")
            store.close()

    def test_emotion_paths_are_relative_and_old_workspace_paths_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            emotion = workspace / "emotion"
            emotion.mkdir(parents=True)
            asset = emotion / "asset.gif"
            asset.write_bytes(b"gif")
            database = workspace / "data" / "momoi.sqlite3"
            database.parent.mkdir()
            old = "/old/momoi/config/emotion/asset.gif"

            store = Store(database, workspace)
            now = time.time()
            store._db.execute(
                """INSERT INTO emotions(slug, path, description, created_at, updated_at)
                   VALUES ('salute', ?, '敬礼', ?, ?)""",
                (old, now, now),
            )
            payload = json.dumps(
                {
                    "action": "message",
                    "segments": [{"type": "image", "data": {"file": old}}],
                }
            )
            store._db.execute(
                """INSERT INTO outbox
                   (turn_id, dedupe_key, text, state, attempts, last_error,
                    kind, media_path, payload_json)
                   VALUES ('turn', 'emotion', 'emotion://salute', 'failed', 1,
                           'media asset cannot be read: FileNotFoundError',
                           'image', ?, ?)""",
                (old, payload),
            )
            store._db.commit()
            store.close()

            migrated = Store(database, workspace)
            raw_path = migrated._db.execute(
                "SELECT path FROM emotions WHERE slug='salute'"
            ).fetchone()[0]
            raw_outbox = migrated._db.execute(
                "SELECT state, media_path, payload_json FROM outbox WHERE dedupe_key='emotion'"
            ).fetchone()
            self.assertEqual(raw_path, "emotion/asset.gif")
            self.assertEqual(raw_outbox["media_path"], "emotion/asset.gif")
            self.assertIn("emotion/asset.gif", raw_outbox["payload_json"])
            self.assertEqual(raw_outbox["state"], "pending")
            due = migrated.due_outbox()[0]
            self.assertEqual(due.media_path, str(asset.resolve()))
            self.assertEqual(
                due.payload["segments"][0]["data"]["file"], str(asset.resolve())
            )
            migrated.close()

    def test_tool_audit_reuses_completed_call_and_blocks_ambiguous_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            self.assertIsNone(
                store.begin_tool_call("turn-1", "call-1", "curl", {"url": "http://x"})
            )
            store.complete_tool_call("turn-1", "call-1", {"ok": True, "status": 200})
            self.assertEqual(
                store.begin_tool_call("turn-1", "call-1", "curl", {"url": "http://x"}),
                {"ok": True, "status": 200},
            )
            self.assertEqual(
                store.begin_tool_call("turn-1", "call-1", "curl", {"url": "http://y"})[
                    "error"
                ],
                "tool_call_id_conflict",
            )
            self.assertIsNone(
                store.begin_tool_call("turn-1", "call-2", "write_file", {"path": "/x"})
            )
            replay = store.begin_tool_call(
                "turn-1", "call-2", "write_file", {"path": "/x"}
            )
            self.assertTrue(replay["ambiguous"])
            store.close()

    def test_legacy_context_manifest_tables_are_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            database = sqlite3.connect(path)
            database.executescript(
                "CREATE TABLE context_manifests(id INTEGER);"
                "INSERT INTO context_manifests VALUES (1);"
                "CREATE TABLE context_blobs(id INTEGER);"
            )
            database.close()

            store = Store(path)
            count = store._db.execute(
                "SELECT COUNT(*) FROM context_manifests"
            ).fetchone()[0]
            self.assertEqual(count, 1)
            store.close()

    def test_crashed_external_effect_turn_requires_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            self.assertEqual(
                store.begin_turn("turn-crash", "owner", ["qq:1:crash"]),
                "running",
            )
            self.assertIsNone(
                store.begin_tool_call(
                    "turn-crash", "call-crash", "curl", {"url": "http://x"}
                )
            )
            store.close()

            recovered = Store(path)
            self.assertEqual(
                recovered.begin_turn("turn-crash", "owner", ["qq:1:crash"]),
                "needs_reconciliation",
            )
            turn = recovered._db.execute(
                "SELECT stage, failure_reason FROM turns WHERE id='turn-crash'"
            ).fetchone()
            self.assertEqual(turn["stage"], "needs_reconciliation")
            self.assertEqual(
                turn["failure_reason"],
                "process_interrupted_after_external_effect",
            )
            self.assertIn("turn-crash", recovered.open_reconciliations_context())
            recovered.close()

    def test_read_only_tool_crash_does_not_require_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store.begin_turn("turn-read", "owner", ["qq:1:read"])
            self.assertIsNone(
                store.begin_tool_call(
                    "turn-read",
                    "call-read",
                    "read_file",
                    {"path": "/tmp/read-only"},
                    "read",
                )
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT capability FROM tool_audit WHERE turn_id='turn-read'"
                ).fetchone()[0],
                "read",
            )
            store.record_turn_failure("turn-read", "ProviderError")
            store.record_turn_usage("turn-read", 100, 20)
            store._db.execute(
                "UPDATE turns SET stage='llm', started_at=1 WHERE id='turn-read'"
            )
            store._db.commit()
            store.close()

            recovered = Store(path)
            before_retry = time.time()
            self.assertEqual(
                recovered.begin_turn("turn-read", "owner", ["qq:1:read"]),
                "running",
            )
            turn = recovered._db.execute(
                """SELECT stage, failure_reason, llm_calls, input_tokens,
                          output_tokens, started_at
                   FROM turns WHERE id='turn-read'"""
            ).fetchone()
            self.assertEqual(turn["stage"], "started")
            self.assertIsNone(turn["failure_reason"])
            self.assertEqual(turn["llm_calls"], 0)
            self.assertEqual(turn["input_tokens"], 0)
            self.assertEqual(turn["output_tokens"], 0)
            self.assertGreaterEqual(turn["started_at"], before_retry)
            self.assertEqual(recovered.open_reconciliations_context(), "")
            recovered.close()

        self.assertEqual(
            BuiltinTools.capability(ToolCall("get", "curl", {"method": "GET"})),
            "read",
        )
        self.assertEqual(
            BuiltinTools.capability(ToolCall("post", "curl", {"method": "POST"})),
            "external_effect",
        )
        self.assertEqual(
            BuiltinTools.capability(ToolCall("write", "write_file", {})),
            "write",
        )

    def test_owner_can_resolve_or_resume_open_reconciliation_by_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            turn_id = "a" * 32
            daemon.store.open_reconciliation(turn_id, "unknown_external_result")
            command = IncomingMessage(
                "qq:1:resume",
                "resume",
                f"/resume {turn_id[:12]} 设备确认没有打开，请继续",
                1,
                1,
            )
            result = daemon._apply_reconciliation_commands([command])
            self.assertIn("status=resumed", result)
            self.assertIn("设备确认没有打开，请继续", result)
            self.assertEqual(daemon.store.open_reconciliations_context(), "")
            with self.assertRaisesRegex(ValueError, "not found"):
                daemon.store.resolve_reconciliation(
                    turn_id[:12], "重复确认", resume=False
                )
            daemon.store.close()

    def test_goal_is_persisted_claimed_and_rescheduled_with_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage("qq:1:goal", "goal", "十分钟后继续检查", 1, 1)
            store.add_event(event)
            draft = TurnDraft()
            created = tools.execute(
                ToolCall(
                    "goal-create",
                    "goal_create",
                    {
                        "title": "检查任务",
                        "success_criteria": "确认检查完成",
                        "next_action": "再次检查",
                        "next_review_at": (
                            datetime.now().astimezone() + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            self.assertTrue(created["ok"])
            goal_id = created["goal"]["id"]
            store.commit_turn([event], event.text, AgentReply(["接下了"]), draft)
            time.sleep(0.03)
            self.assertEqual(store.claim_due_goal()["id"], goal_id)

            autonomous = TurnDraft()
            updated = tools.execute(
                ToolCall(
                    "goal-update",
                    "goal_update",
                    {
                        "goal_id": goal_id,
                        "status": "waiting",
                        "waiting_for": "下一次检查时间",
                        "latest_result": "本次检查正常",
                        "next_review_at": (
                            datetime.now().astimezone() + timedelta(hours=1)
                        ).isoformat(),
                    },
                ),
                autonomous,
                authority="agent",
                source_event_id=f"goal:{goal_id}",
                allow_notify=True,
            )
            self.assertTrue(updated["ok"])
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "notify",
                        "owner_notify",
                        {
                            "text": "检查完成，目前正常",
                            "reason": "任务阶段结果",
                            "key": "service.check",
                        },
                    ),
                    autonomous,
                    authority="agent",
                    source_event_id=f"goal:{goal_id}",
                    allow_notify=True,
                )["ok"]
            )
            store.commit_autonomous_turn(goal_id, autonomous)
            self.assertEqual(store.goal(goal_id)["status"], "waiting")
            first = store.due_outbox()[0]
            store.mark_sent(first.id)
            notification = store.claim_due_notification(NotificationConfig())
            self.assertIsNotNone(notification)
            self.assertTrue(store.queue_notification(str(notification["id"])))
            self.assertEqual(store.due_outbox()[0].text, "检查完成，目前正常")
            store.close()

    def test_one_time_reminder_fires_once_and_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage("qq:1:reminder", "reminder", "提醒我活动", 1, 1)
            store.add_event(event)
            draft = TurnDraft()
            created = tools.execute(
                ToolCall(
                    "reminder-create",
                    "reminder_create",
                    {
                        "text": "该起来活动一下啦",
                        "fire_at": (
                            datetime.now().astimezone() + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            self.assertTrue(created["ok"])
            reminder_id = created["reminder"]["id"]
            store.commit_turn([event], event.text, AgentReply(["好"]), draft)
            time.sleep(0.03)
            claimed = store.claim_due_reminder()
            self.assertEqual(claimed["id"], reminder_id)
            self.assertTrue(store.fire_reminder(reminder_id))
            self.assertFalse(store.fire_reminder(reminder_id))
            first = store.due_outbox()[0]
            store.mark_sent(first.id)
            self.assertEqual(store.due_outbox()[0].text, "该起来活动一下啦")
            self.assertEqual(store.reminder(reminder_id)["status"], "fired")

            cancel_event = IncomingMessage(
                "qq:1:cancel-reminder", "cancel-reminder", "取消提醒", 2, 2
            )
            store.add_event(cancel_event)
            create_cancelled = TurnDraft()
            pending = tools.execute(
                ToolCall(
                    "reminder-create-2",
                    "reminder_create",
                    {
                        "text": "这条不该发送",
                        "fire_at": (
                            datetime.now().astimezone() + timedelta(hours=1)
                        ).isoformat(),
                    },
                ),
                create_cancelled,
                authority="owner",
                source_event_id=cancel_event.event_id,
                allow_notify=False,
            )["reminder"]
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "reminder-cancel",
                        "reminder_cancel",
                        {"reminder_id": pending["id"]},
                    ),
                    create_cancelled,
                    authority="owner",
                    source_event_id=cancel_event.event_id,
                    allow_notify=False,
                )["ok"]
            )
            store.commit_turn(
                [cancel_event],
                cancel_event.text,
                AgentReply(["取消了"]),
                create_cancelled,
            )
            self.assertEqual(store.reminder(pending["id"])["status"], "cancelled")
            store.close()

    def test_mood_transition_persists_and_settles_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:mood", "mood", "今天真开心", 1, 1)
            store.add_event(event)
            store.commit_turn(
                [event],
                event.text,
                AgentReply(
                    ["我也是！"],
                    mood_transition={
                        "state": "excited",
                        "intensity": 0.8,
                        "cause": "一起分享了开心的事",
                        "duration_minutes": 30,
                    },
                ),
            )
            active = store.self_state()
            self.assertEqual(active["mood_state"], "excited")
            settled = store.self_state(float(active["mood_settle_at"]) + 1)
            self.assertEqual(settled["mood_state"], "calm")
            self.assertEqual(settled["mood_intensity"], 0.35)
            self.assertEqual(settled["mood_cause"], "resting baseline")
            self.assertIsNone(settled["mood_settle_at"])
            store.close()

    def test_old_default_self_state_migrates_to_neutral_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store._db.execute(
                """UPDATE self_state
                   SET mood_state='cheerful', mood_intensity=0.55,
                       mood_cause='personality baseline', mood_settle_at=NULL,
                       activity='自由安排自己的时间'
                   WHERE id=1"""
            )
            store._db.commit()
            store.close()

            store = Store(path)
            state = store.self_state()
            self.assertEqual(state["mood_state"], "calm")
            self.assertEqual(state["mood_intensity"], 0.35)
            self.assertEqual(state["mood_cause"], "resting baseline")
            self.assertEqual(state["activity"], "spending time freely")
            store.close()

    def test_recurring_reminder_fires_multiple_occurrences(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=1000),
        ):
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage("qq:1:repeat", "repeat", "每分钟提醒", 1, 1)
            store.add_event(event)
            draft = TurnDraft()
            reminder = tools.execute(
                ToolCall(
                    "reminder-repeat",
                    "reminder_create",
                    {
                        "text": "喝口水啦",
                        "schedule": {
                            "kind": "interval",
                            "every_seconds": 60,
                            "timezone": "Asia/Shanghai",
                        },
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )["reminder"]
            store.commit_turn([event], event.text, AgentReply(["好"]), draft)

            with patch("momoi.storage.store.time.time", return_value=1061):
                self.assertEqual(store.claim_due_reminder()["id"], reminder["id"])
                self.assertTrue(store.fire_reminder(reminder["id"]))
            with patch("momoi.storage.store.time.time", return_value=1122):
                self.assertEqual(store.claim_due_reminder()["id"], reminder["id"])
                self.assertTrue(store.fire_reminder(reminder["id"]))

            rows = store._db.execute(
                "SELECT dedupe_key FROM outbox WHERE turn_id LIKE 'reminder:%' ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["dedupe_key"], rows[1]["dedupe_key"])
            self.assertEqual(store.reminder(reminder["id"])["status"], "pending")
            store.close()

    def test_recurring_reminder_waits_until_quiet_hours_end(self) -> None:
        zone = ZoneInfo("Asia/Shanghai")
        due = datetime(2026, 7, 21, 23, 30, tzinfo=zone).timestamp()
        quiet_end = datetime(2026, 7, 22, 8, 0, tzinfo=zone).timestamp()
        policy = NotificationConfig(
            timezone="Asia/Shanghai", quiet_start="23:00", quiet_end="08:00"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=due),
        ):
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """INSERT INTO reminders
                   (id, text, source_event_id, status, fire_at, schedule_json,
                    created_at, updated_at)
                   VALUES ('quiet-repeat', '喝水', 'test', 'pending', ?, ?, ?, ?)""",
                (
                    due,
                    json.dumps(
                        {
                            "kind": "interval",
                            "every_seconds": 3600,
                            "timezone": "Asia/Shanghai",
                        }
                    ),
                    due,
                    due,
                ),
            )
            store._db.commit()
            self.assertIsNotNone(store.claim_due_reminder())
            self.assertFalse(store.fire_reminder("quiet-repeat", policy))
            self.assertEqual(store.reminder("quiet-repeat")["fire_at"], quiet_end)
            self.assertEqual(store.due_outbox(), [])
            with patch("momoi.storage.store.time.time", return_value=quiet_end):
                self.assertIsNotNone(store.claim_due_reminder())
                self.assertTrue(store.fire_reminder("quiet-repeat", policy))
            self.assertEqual(store.due_outbox()[0].text, "喝水")
            store.close()

    def test_heartbeat_daily_budget_moves_next_check_to_next_day(self) -> None:
        now = datetime(2026, 7, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        heartbeat = HeartbeatConfig(
            enabled=True,
            initial_delay_seconds=60,
            min_interval_seconds=60,
            max_interval_seconds=600,
            max_daily_turns=1,
        )
        notifications = NotificationConfig(timezone="Asia/Shanghai")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=now),
        ):
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("heartbeat-budget", "autonomous", ["heartbeat:test"])
            store.commit_heartbeat(
                "heartbeat-budget",
                activity="整理关卡灵感",
                next_heartbeat_at=now + 60,
                mood_transition=None,
                messages=[],
                reason="test",
                timezone="Asia/Shanghai",
            )
            with patch("momoi.storage.store.time.time", return_value=now + 61):
                self.assertIsNone(store.claim_due_heartbeat(heartbeat, notifications))
            state = store.self_state(now + 61)
            next_day = datetime(2026, 7, 22, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(state["next_heartbeat_at"], next_day.timestamp())
            store.close()

    def test_recurring_goal_persists_schedule_and_advances_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage(
                "qq:1:recurring", "recurring", "每小时检查一次服务", 1, 1
            )
            store.add_event(event)
            draft = TurnDraft()
            created = tools.execute(
                ToolCall(
                    "goal-recurring",
                    "goal_create",
                    {
                        "title": "检查服务",
                        "success_criteria": "每次确认服务状态",
                        "next_action": "检查服务状态",
                        "schedule": {
                            "kind": "interval",
                            "every_seconds": 3600,
                            "timezone": "Asia/Shanghai",
                        },
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            self.assertTrue(created["ok"], created)
            goal_id = created["goal"]["id"]
            store.commit_turn([event], event.text, AgentReply(["会定期检查"]), draft)
            self.assertEqual(store.goal(goal_id)["schedule"]["kind"], "interval")

            store._db.execute(
                "UPDATE goals SET next_review_at=? WHERE id=?",
                (time.time() - 1, goal_id),
            )
            store._db.commit()
            self.assertEqual(store.claim_due_goal()["id"], goal_id)
            before = time.time()
            store.commit_autonomous_turn(goal_id, TurnDraft())
            advanced = store.goal(goal_id)
            self.assertEqual(advanced["status"], "active")
            self.assertGreaterEqual(advanced["next_review_at"], before + 3599)
            self.assertIsNone(advanced["review_claimed_at"])

            after = datetime(2026, 7, 20, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
            next_daily = next_schedule_at(
                {"kind": "daily", "at": "08:00", "timezone": "Asia/Shanghai"},
                after.timestamp(),
            )
            self.assertEqual(
                datetime.fromtimestamp(next_daily, ZoneInfo("Asia/Shanghai")),
                datetime(2026, 7, 21, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            store.close()

    def test_notification_policy_delays_without_replaying_goal_work(self) -> None:
        def add(
            store: Store,
            notification_id: str,
            key: str,
            priority: str,
            created_at: float,
        ) -> None:
            store._db.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at)
                   VALUES (?, ?, 'goal', ?, ?, 'test', '[\"状态更新\"]',
                           'pending', ?, ?)""",
                (
                    notification_id,
                    f"turn:{notification_id}",
                    key,
                    priority,
                    created_at,
                    created_at,
                ),
            )
            store._db.commit()

        zone = ZoneInfo("Asia/Shanghai")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            quiet = NotificationConfig(
                timezone="Asia/Shanghai",
                quiet_start="23:00",
                quiet_end="08:00",
                cooldown_seconds=3600,
                daily_budget=10,
            )
            late = datetime(2030, 1, 1, 23, 30, tzinfo=zone).timestamp()
            add(store, "quiet", "service.status", "normal", late)
            self.assertIsNone(store.claim_due_notification(quiet, late))
            morning = datetime(2030, 1, 2, 8, 0, tzinfo=zone).timestamp()
            claimed = store.claim_due_notification(quiet, morning)
            self.assertEqual(claimed["id"], "quiet")
            self.assertTrue(store.queue_notification("quiet", morning, quiet))

            add(store, "cooldown", "service.status", "normal", morning + 60)
            self.assertIsNone(store.claim_due_notification(quiet, morning + 60))
            not_before = store._db.execute(
                "SELECT not_before FROM notifications WHERE id='cooldown'"
            ).fetchone()[0]
            self.assertGreaterEqual(not_before, morning + 3600)

            add(store, "budget", "another.status", "normal", morning + 120)
            budget = NotificationConfig(
                timezone="Asia/Shanghai", cooldown_seconds=0, daily_budget=1
            )
            self.assertIsNone(store.claim_due_notification(budget, morning + 120))
            next_day = datetime(2030, 1, 3, 0, 0, tzinfo=zone).timestamp()
            budget_due = store._db.execute(
                "SELECT not_before FROM notifications WHERE id='budget'"
            ).fetchone()[0]
            self.assertEqual(budget_due, next_day)
            store.close()

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(2030, 1, 2, 10, 0, tzinfo=zone).timestamp()
            pending = IncomingMessage("qq:pending", "pending", "主人消息", now, now)
            store.add_event(pending)
            add(store, "normal", "normal.status", "normal", now)
            add(store, "urgent", "urgent.failure", "urgent", now)
            policy = NotificationConfig(
                timezone="Asia/Shanghai",
                cooldown_seconds=0,
                daily_budget=10,
                urgent_daily_budget=2,
                pending_owner_delay_seconds=60,
            )
            self.assertIsNone(store.claim_due_notification(policy, now))
            claimed = store.claim_due_notification(policy, now)
            self.assertEqual(claimed["id"], "urgent")
            self.assertTrue(store.queue_notification("urgent", now, policy))
            normal_due = store._db.execute(
                "SELECT not_before FROM notifications WHERE id='normal'"
            ).fetchone()[0]
            self.assertEqual(normal_due, now + 60)
            store.close()

    def test_event_turn_and_outbox_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:2", "2", "你好", 1.0, 2.0)
            self.assertTrue(store.add_event(event))
            self.assertFalse(store.add_event(event))
            self.assertEqual(len(store.pending_events()), 1)
            store.commit_turn(
                [event],
                "你好",
                AgentReply(["嘿嘿，没忘吧~", "晚上在忙什么呢？"]),
            )
            self.assertEqual(store.pending_events(), [])
            outbox = store.due_outbox()
            self.assertEqual(len(outbox), 1)
            self.assertEqual(outbox[0].text, "嘿嘿，没忘吧~")
            store.mark_sent(outbox[0].id)
            self.assertEqual(store.due_outbox()[0].text, "晚上在忙什么呢？")
            store.close()

    def test_structured_channel_messages_are_persisted_in_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:rich-out", "rich-out", "发给我", 1, 1)
            store.add_event(event)
            store.commit_turn(
                [event],
                "发给我",
                AgentReply(
                    [
                        {
                            "segments": [
                                {"type": "reply", "data": {"id": "42"}},
                                {"type": "text", "data": {"text": "收到"}},
                                {
                                    "type": "image",
                                    "data": {"file": "https://img.example/a.jpg"},
                                },
                            ]
                        }
                    ]
                ),
            )
            row = store.due_outbox()[0]
            self.assertEqual(row.kind, "message")
            self.assertEqual(row.payload["action"], "message")
            self.assertEqual(
                [part["type"] for part in row.payload["segments"]],
                ["reply", "text", "image"],
            )
            history = store._db.execute(
                "SELECT content FROM messages WHERE role='assistant'"
            ).fetchone()[0]
            self.assertIn("reply to message_id=42", history)
            self.assertIn("https://img.example/a.jpg", history)
            store.close()

    def test_progress_message_is_queued_before_final_reply_and_committed_to_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:progress", "progress", "执行任务", 1, 1)
            store.add_event(event)
            store.begin_turn("turn-progress", "owner", [event.event_id])
            store.queue_progress("turn-progress", "call-progress", ["正在处理"])
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["处理完成"]),
                turn_id="turn-progress",
            )

            self.assertEqual(
                [item["content"] for item in store.history(1000, 1)],
                ["执行任务", "正在处理", "处理完成"],
            )
            first = store.due_outbox()[0]
            self.assertEqual(first.text, "正在处理")
            store.mark_sent(first.id)
            self.assertEqual(store.due_outbox()[0].text, "处理完成")
            store.close()

    def test_structured_continuity_tracks_source_and_expires_short_term_facts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:continuity", "continuity", "继续聊", 1, 1)
            store.add_event(event)
            store.commit_turn(
                [event],
                event.text,
                AgentReply(
                    ["好"],
                    {
                        "topic": "当前话题",
                        "open_loops": ["等待结果"],
                        "pending_commitments": [],
                        "short_term_facts": [
                            {
                                "text": "仍然有效",
                                "expires_at": (
                                    datetime.now().astimezone() + timedelta(hours=1)
                                ).isoformat(),
                            },
                            {
                                "text": "已经过期",
                                "expires_at": (
                                    datetime.now().astimezone() - timedelta(hours=1)
                                ).isoformat(),
                            },
                        ],
                    },
                ),
            )
            state = json.loads(store.continuity())
            self.assertEqual(state["topic"]["source_event_ids"], [event.event_id])
            self.assertEqual(
                [fact["text"] for fact in state["short_term_facts"]], ["仍然有效"]
            )
            context = json.loads(store.continuity_context())
            self.assertEqual(
                context,
                {
                    "topic": "当前话题",
                    "open_loops": ["等待结果"],
                    "pending_commitments": [],
                    "short_term_facts": [
                        {
                            "text": "仍然有效",
                            "expires_at": state["short_term_facts"][0]["expires_at"],
                        }
                    ],
                },
            )
            store.close()

    def test_memory_survives_history_window_and_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            event = IncomingMessage(
                "qq:1:memory-1",
                "memory-1",
                "以后卧室灯都用暖色，记住哦",
                1.0,
                1.0,
            )
            store.add_event(event)
            draft = TurnDraft()
            result = MemoryTools(store).execute(
                ToolCall(
                    "tool-1",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.bedroom.light_color",
                        "content": "卧室灯默认使用暖色",
                        "evidence": "卧室灯都用暖色",
                        "importance": 0.9,
                    },
                ),
                [event],
                draft,
            )
            self.assertTrue(result["ok"])
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["记住啦"], "卧室灯光偏好已经确认。"),
                draft,
            )
            self.assertEqual(
                json.loads(store.continuity())["topic"]["text"],
                "卧室灯光偏好已经确认。",
            )
            self.assertIn(
                "卧室灯默认使用暖色",
                store.memory_context("灯光按我喜欢的来", 6, 8000),
            )

            correction = IncomingMessage(
                "qq:1:memory-2",
                "memory-2",
                "改一下，以后卧室灯用冷色",
                2.0,
                2.0,
            )
            store.add_event(correction)
            correction_draft = TurnDraft()
            result = MemoryTools(store).execute(
                ToolCall(
                    "tool-2",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.bedroom.light_color",
                        "content": "卧室灯默认使用冷色",
                        "evidence": "卧室灯用冷色",
                        "importance": 0.9,
                        "replace_confirmed": True,
                    },
                ),
                [correction],
                correction_draft,
            )
            self.assertTrue(result["ok"])
            store.commit_turn(
                [correction],
                correction.text,
                AgentReply(["改成冷色了"], ""),
                correction_draft,
            )
            recalled = store.memory_context("卧室灯光", 6, 8000)
            self.assertIn("卧室灯默认使用冷色", recalled)
            self.assertNotIn("卧室灯默认使用暖色", recalled)

            for index in range(30):
                item = IncomingMessage(
                    f"qq:1:{index + 10}",
                    str(index + 10),
                    f"第{index}轮",
                    float(index + 10),
                    float(index + 10),
                )
                store.add_event(item)
                store.commit_turn([item], item.text, AgentReply([f"回复{index}"]))
            self.assertGreater(len(store.history(10000, 6)), 24)
            store.close()
            store = Store(path)
            self.assertIn(
                "卧室灯默认使用冷色",
                store.memory_context("卧室灯光", 6, 8000),
            )
            store.close()

    def test_uncertain_memory_conflict_waits_for_owner_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            original = IncomingMessage(
                "qq:1:original", "original", "记住我喜欢暖色灯", 1, 1
            )
            store.add_event(original)
            draft = TurnDraft()
            tools.execute(
                ToolCall(
                    "remember-original",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.light.color",
                        "content": "主人喜欢暖色灯",
                        "evidence": "我喜欢暖色灯",
                    },
                ),
                [original],
                draft,
            )
            store.commit_turn([original], original.text, AgentReply(["记住了"]), draft)

            uncertain = IncomingMessage(
                "qq:1:uncertain", "uncertain", "也许我更喜欢冷色灯", 2, 2
            )
            store.add_event(uncertain)
            conflict_draft = TurnDraft()
            result = tools.execute(
                ToolCall(
                    "remember-uncertain",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.light.color",
                        "content": "主人喜欢冷色灯",
                        "evidence": "也许我更喜欢冷色灯",
                    },
                ),
                [uncertain],
                conflict_draft,
            )
            self.assertEqual(result["state"], "conflict_pending")
            store.commit_turn(
                [uncertain],
                uncertain.text,
                AgentReply(["你想改成冷色吗"]),
                conflict_draft,
            )
            self.assertEqual(
                store.active_memory("preference", "home.light.color")["content"],
                "主人喜欢暖色灯",
            )
            self.assertIn("candidate=主人喜欢冷色灯", store.memory_conflicts_context())

            confirmed = IncomingMessage(
                "qq:1:confirmed", "confirmed", "对，改成冷色灯", 3, 3
            )
            store.add_event(confirmed)
            confirmed_draft = TurnDraft()
            result = tools.execute(
                ToolCall(
                    "remember-confirmed",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.light.color",
                        "content": "主人喜欢冷色灯",
                        "evidence": "改成冷色灯",
                        "replace_confirmed": True,
                    },
                ),
                [confirmed],
                confirmed_draft,
            )
            self.assertEqual(result["state"], "staged")
            store.commit_turn(
                [confirmed], confirmed.text, AgentReply(["已经改好了"]), confirmed_draft
            )
            self.assertEqual(
                store.active_memory("preference", "home.light.color")["content"],
                "主人喜欢冷色灯",
            )
            self.assertEqual(store.memory_conflicts_context(), "")
            store.close()

    def test_repeated_identical_memory_updates_evidence_without_duplication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            for index, text in enumerate(
                ["记住，我喜欢暖色灯", "再说一次，我喜欢暖色灯"], start=1
            ):
                event = IncomingMessage(
                    f"qq:1:same-memory-{index}",
                    f"same-memory-{index}",
                    text,
                    float(index),
                    float(index),
                )
                store.add_event(event)
                draft = TurnDraft()
                result = tools.execute(
                    ToolCall(
                        f"remember-{index}",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "home.light.color",
                            "content": "主人偏好暖色灯光",
                            "evidence": "我喜欢暖色灯",
                            "importance": 0.7 + index / 10,
                        },
                    ),
                    [event],
                    draft,
                )
                self.assertTrue(result["ok"])
                store.commit_turn([event], text, AgentReply(["记住了"]), draft)

            rows = store._db.execute(
                "SELECT id, source_event_id, evidence_quote, importance FROM memories"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_event_id"], "qq:1:same-memory-2")
            self.assertEqual(rows[0]["evidence_quote"], "我喜欢暖色灯")
            self.assertAlmostEqual(rows[0]["importance"], 0.9)
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM memory_evidence WHERE memory_id=?",
                    (rows[0]["id"],),
                ).fetchone()[0],
                2,
            )
            store.close()

    def test_memory_forget_requires_owner_evidence_and_can_be_relearned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            remembered = IncomingMessage(
                "qq:1:remember-forget", "remember-forget", "记住我喜欢暖色", 1, 1
            )
            store.add_event(remembered)
            draft = TurnDraft()
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "remember",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "content": "主人喜欢暖色",
                            "evidence": "我喜欢暖色",
                        },
                    ),
                    [remembered],
                    draft,
                )["ok"]
            )
            store.commit_turn(
                [remembered], remembered.text, AgentReply(["记住了"]), draft
            )

            uncertain = IncomingMessage(
                "qq:1:forget-conflict", "forget-conflict", "也许我喜欢冷色", 1.5, 1.5
            )
            store.add_event(uncertain)
            conflict_draft = TurnDraft()
            self.assertEqual(
                tools.execute(
                    ToolCall(
                        "remember-conflict",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "content": "主人喜欢冷色",
                            "evidence": "也许我喜欢冷色",
                        },
                    ),
                    [uncertain],
                    conflict_draft,
                )["state"],
                "conflict_pending",
            )
            store.commit_turn(
                [uncertain], uncertain.text, AgentReply(["需要你确认"]), conflict_draft
            )
            self.assertTrue(store.memory_conflicts_context())

            forgotten = IncomingMessage(
                "qq:1:forget", "forget", "忘掉灯光颜色偏好", 2, 2
            )
            store.add_event(forgotten)
            invalid = tools.execute(
                ToolCall(
                    "forget-invalid",
                    "memory_forget",
                    {
                        "kind": "preference",
                        "key": "light.color",
                        "evidence": "这段话并不存在",
                    },
                ),
                [forgotten],
                TurnDraft(),
            )
            self.assertEqual(invalid["error"], "evidence_not_in_current_input")

            forget_draft = TurnDraft()
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "forget",
                        "memory_forget",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "evidence": "忘掉灯光颜色偏好",
                        },
                    ),
                    [forgotten],
                    forget_draft,
                )["ok"]
            )
            store.commit_turn(
                [forgotten], forgotten.text, AgentReply(["已经忘掉了"]), forget_draft
            )
            self.assertEqual(store.search_memories("灯光颜色", 6), [])
            self.assertEqual(store.memory_conflicts_context(), "")
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1
            )

            relearned = IncomingMessage(
                "qq:1:relearn", "relearn", "重新记住我喜欢冷色", 3, 3
            )
            store.add_event(relearned)
            relearn_draft = TurnDraft()
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "relearn",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "content": "主人喜欢冷色",
                            "evidence": "我喜欢冷色",
                        },
                    ),
                    [relearned],
                    relearn_draft,
                )["ok"]
            )
            store.commit_turn(
                [relearned], relearned.text, AgentReply(["重新记住了"]), relearn_draft
            )
            self.assertIn("主人喜欢冷色", store.memory_context("灯光颜色", 6, 1000))
            store.close()

    def test_memory_remember_validates_boundary_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            evidence = "证" * 500
            event = IncomingMessage("qq:1:bounds", "bounds", evidence, 1, 1)
            valid = {
                "kind": "preference",
                "key": "k" * 200,
                "content": "内" * 2000,
                "evidence": evidence,
                "importance": 2,
            }
            draft = TurnDraft()
            accepted = tools.execute(
                ToolCall("valid", "memory_remember", valid), [event], draft
            )
            self.assertTrue(accepted["ok"])
            self.assertEqual(draft.memories[0].importance, 1.0)

            invalid = [
                ({**valid, "kind": "unknown"}, "invalid_kind"),
                ({**valid, "key": "UPPER"}, "invalid_key"),
                ({**valid, "key": "k" * 201}, "invalid_key"),
                ({**valid, "content": ""}, "invalid_content"),
                ({**valid, "content": "内" * 2001}, "invalid_content"),
                (
                    {**valid, "evidence": "不在当前消息里"},
                    "evidence_not_in_current_input",
                ),
                ({**valid, "evidence": "证" * 501}, "evidence_not_in_current_input"),
                ({**valid, "replace_confirmed": "yes"}, "invalid_replace_confirmed"),
            ]
            for index, (arguments, error) in enumerate(invalid):
                result = tools.execute(
                    ToolCall(f"invalid-{index}", "memory_remember", arguments),
                    [event],
                    TurnDraft(),
                )
                self.assertEqual(result["error"], error)
            store.close()

    def test_compaction_keeps_raw_messages_and_replaces_old_context_with_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for index in range(4):
                event = IncomingMessage(
                    f"qq:1:compact-{index}",
                    f"compact-{index}",
                    f"第{index}轮" + "内容" * 100,
                    float(index),
                    float(index),
                )
                store.add_event(event)
                store.commit_turn(
                    [event], event.text, AgentReply(["回复" + "内容" * 100])
                )

            candidate = store.compaction_candidate(250, 1)
            self.assertIsNotNone(candidate)
            rows, start_id, end_id = candidate
            self.assertGreater(len(rows), 0)
            store.save_conversation_summary("较早对话摘要", start_id, end_id)

            self.assertIn("较早对话摘要", store.summary_context("没有关键词", 3, 1000))
            search = MemoryTools(store).execute(
                ToolCall(
                    "conversation-search",
                    "conversation_search",
                    {"query": "较早对话"},
                ),
                [],
                TurnDraft(),
            )
            self.assertEqual(search["count"], 1)
            read = MemoryTools(store).execute(
                ToolCall(
                    "conversation-read",
                    "conversation_read",
                    {"segment_id": search["results"][0]["id"]},
                ),
                [],
                TurnDraft(),
            )
            self.assertTrue(read["ok"])
            self.assertGreater(len(read["segment"]["messages"]), 0)
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 8
            )
            self.assertTrue(
                all(
                    "第3轮" in item["content"] or item["role"] == "assistant"
                    for item in store.history(250, 1)
                )
            )
            store.close()
