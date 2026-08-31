import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime import MomoiDaemon


class ReplyWaitNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_followup_continues_after_native_shared_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage("reply:event", "1", "晚上选个游戏吧", 1, 1)
            daemon.store.add_event(event)
            owner_turn = daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["那你想玩解谜还是动作呀"]),
            )
            outbox_id = daemon.store._db.execute(
                "SELECT id FROM outbox WHERE turn_id=?", (owner_turn,)
            ).fetchone()["id"]
            daemon.store.mark_sent(int(outbox_id))
            daemon.store._db.execute(
                """UPDATE self_state
                   SET pending_reply_turn_id=?,
                       pending_reply_expectation='主人对问题的回答',
                       pending_reply_since=1000,
                       pending_reply_last_reason='这个问题需要老师决定',
                       pending_reply_delay_minutes=4,
                       pending_reply_next_check_at=1240
                   WHERE id=1""",
                (owner_turn,),
            )
            daemon.store._db.commit()

            terminal = AgentReply([], reply_wait={"wait": False})
            with (
                patch.object(
                    daemon,
                    "_run_tool_loop",
                    new_callable=AsyncMock,
                    return_value=terminal,
                ) as run,
                patch.object(daemon, "_commit_reply_followup_state"),
            ):
                await daemon._complete_reply_wait(
                    "reply-followup", "napcat", owner_event_revision=1
                )

            messages = run.await_args.args[1]
            rendered = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn("<reply_timeline>", rendered)
            self.assertIn("<followup>", rendered)
            self.assertIn("reason: 这个问题需要老师决定", rendered)
            self.assertRegex(rendered, r"silent_minutes: \d+")
            self.assertEqual(
                [message["role"] for message in messages],
                ["user", "user", "assistant", "user"],
            )
            self.assertIn("晚上选个游戏吧", str(messages[1]["content"]))
            self.assertIn("那你想玩解谜还是动作呀", str(messages[2]["content"]))
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
