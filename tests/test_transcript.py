import time
from zoneinfo import ZoneInfo

from momoi.runtime.transcript import (
    build_groups,
    build_transcript as _build_transcript,
    render_delivered_bubble_evidence,
    render_messages as _render_messages,
    select_groups,
    turn_labels,
)

TEST_TIMEZONE = ZoneInfo("Asia/Shanghai")


def render_messages(*args, **kwargs):
    return _render_messages(*args, timezone=TEST_TIMEZONE, **kwargs)


def build_transcript(*args, **kwargs):
    return _build_transcript(*args, timezone=TEST_TIMEZONE, **kwargs)

BASE = time.mktime((2026, 8, 31, 20, 0, 0, 0, 0, -1))


def text(message: dict[str, object]) -> str:
    return "".join(
        str(block.get("text") or "")
        for block in message["content"]
        if isinstance(block, dict)
    )


def test_turn_labels_are_stable_for_each_runtime_turn():
    groups = build_groups(
        [
            owner(1, "第一问", turn_id="turn-a"),
            bubble(2, "第一答", turn_id="turn-a"),
            owner(3, "第二问", turn_id="turn-b"),
        ]
    )
    labels = turn_labels(groups)
    messages = render_messages(groups, labels=labels)

    assert labels == {"turn-a": "T1", "turn-b": "T2"}
    assert "[turn=T1" in text(messages[0])
    assert "[turn=T1" in text(messages[1])
    assert "[turn=T2" in text(messages[2])


def owner(
    identifier: int, content: str, *, turn_id: str = "t1", offset: float = 0.0
) -> dict[str, object]:
    return {
        "id": identifier,
        "turn_id": turn_id,
        "role": "user",
        "content": content,
        "created_at": BASE + offset,
        "delivery_state": "delivered",
    }


def bubble(
    identifier: int,
    content: str,
    *,
    turn_id: str = "t1",
    offset: float = 0.0,
    delivery_state: str = "delivered",
) -> dict[str, object]:
    return {
        "id": identifier,
        "turn_id": turn_id,
        "role": "assistant",
        "content": content,
        "created_at": BASE + offset,
        "delivery_state": delivery_state,
    }


def test_one_send_bubbles_call_becomes_one_assistant_message():
    groups = build_groups(
        [
            owner(1, "在吗"),
            bubble(2, "在的"),
            bubble(3, "怎么了"),
        ]
    )
    assert [group.role for group in groups] == ["user", "assistant"]
    assert groups[1].parts == ("在的", "怎么了")
    assert groups[1].message_ids == (2, 3)


def test_send_bubbles_calls_around_tool_work_stay_one_assistant_turn():
    groups = build_groups(
        [
            owner(1, "帮我看看"),
            bubble(2, "我看看", turn_id="t1"),
            bubble(3, "好了", turn_id="t1", offset=240),
        ]
    )
    assert [group.role for group in groups] == ["user", "assistant"]
    assert groups[1].parts == ("我看看", "好了")


def test_a_later_spontaneous_message_is_not_folded_into_the_reply():
    groups = build_groups(
        [
            owner(1, "晚安"),
            bubble(2, "晚安", turn_id="t1"),
            bubble(3, "刚想起来一件事", turn_id="hb1", offset=8 * 3600),
        ]
    )
    assert len(groups) == 3
    assert groups[2].turn_ids == ("hb1",)
    messages = render_messages(groups, gap_seconds=1800)
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert text(messages[2]) == "[owner did not reply · 8h later]"
    assert text(messages[3]).startswith("[2026-09-01T04:00")


def test_a_turn_that_deliberately_said_nothing_is_recorded():
    messages = render_messages(
        build_groups(
            [
                owner(1, "到家了", turn_id="t0"),
                owner(2, "在弄晚饭", turn_id="t1", offset=600),
                bubble(3, "好", turn_id="t1", offset=610),
            ]
        )
    )
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert text(messages[1]) == "[ended the Turn without replying]"


def test_progress_and_result_in_one_turn_is_not_an_owner_silence():
    messages = render_messages(
        build_groups(
            [
                owner(1, "帮我查一下"),
                bubble(2, "我看看", turn_id="t1"),
                bubble(3, "查到了", turn_id="t1", offset=240),
            ]
        )
    )
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "did not reply" not in text(messages[1])


def test_repeated_proactive_messages_show_each_unanswered_attempt():
    messages = render_messages(
        build_groups(
            [
                owner(1, "在忙", turn_id="t0"),
                bubble(2, "好", turn_id="t0"),
                bubble(3, "在吗", turn_id="hb1", offset=1800),
                bubble(4, "睡了吗", turn_id="hb2", offset=5400),
                owner(5, "刚看到", turn_id="t1", offset=7200),
            ]
        )
    )
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert text(messages[2]) == "[owner did not reply · 30m later]"
    assert text(messages[4]) == "[owner did not reply · 1h later]"
    assert "刚看到" in text(messages[6])


def test_owner_update_after_a_bubble_keeps_its_position():
    groups = build_groups(
        [
            owner(1, "帮我查一下"),
            bubble(2, "好，我看看"),
            owner(3, "算了，改成明天"),
            bubble(4, "好的"),
        ]
    )
    assert [group.role for group in groups] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert groups[2].parts == ("算了，改成明天",)


def test_insertion_order_wins_over_inverted_timestamps():
    groups = build_groups(
        [bubble(2, "回复", offset=10), owner(1, "提问", offset=99)]
    )
    assert [group.role for group in groups] == ["user", "assistant"]


def test_internal_and_failed_output_is_not_conversation():
    groups = build_groups(
        [
            owner(1, "早"),
            bubble(2, "内部记录", delivery_state="internal"),
            bubble(3, "发送失败", delivery_state="failed"),
            bubble(4, "早上好"),
        ]
    )
    assert len(groups) == 2
    assert groups[1].parts == ("早上好",)


def test_inbound_events_are_not_owner_speech():
    event = owner(1, "webhook payload") | {"role": "event"}
    groups = build_groups([event, owner(2, "看到了")])
    assert [group.role for group in groups] == ["user"]


def test_delivered_autonomous_speech_is_assistant_history():
    groups = build_groups(
        [
            bubble(1, "我刚看到一条新闻", turn_id="hb1"),
            owner(2, "什么新闻", turn_id="t2"),
        ]
    )
    assert [group.role for group in groups] == ["assistant", "user"]
    assert groups[0].turn_ids == ("hb1",)


def action(name: str, *, at: float, subject: str = "", ok: bool = True, ref: str = ""):
    return {
        "at": BASE + at,
        "name": name,
        "subject": subject,
        "ok": ok,
        "error": "" if ok else "timed out",
        "ref": ref,
    }


def test_work_is_interleaved_with_the_words_that_narrate_it():
    messages = render_messages(
        build_groups(
            [
                owner(1, "刷微博"),
                bubble(2, "好的，我刷微博", offset=1),
                bubble(3, "我发现了内容XXX", offset=3),
                bubble(4, "刷完了", offset=5),
            ]
        ),
        tool_activity={
            "t1": [
                action("weibo_feed", at=2, subject="home"),
                action("weibo_detail", at=4, subject="4012", ref="tr-9"),
            ]
        },
    )
    assert text(messages[1]).split("\n") == [
        "好的，我刷微博",
        "[tool_call] weibo_feed(home) -> ok",
        "我发现了内容XXX",
        "[tool_call] weibo_detail(4012) -> ok · ref=tr-9",
        "刷完了",
    ]


def test_a_failed_call_cannot_hide_behind_a_confident_reply():
    messages = render_messages(
        build_groups([owner(1, "查一下"), bubble(2, "查好了", offset=2)]),
        tool_activity={"t1": [action("curl", at=1, subject="http://x", ok=False)]},
    )
    assert "[tool_call] curl(http://x) -> failed: timed out" in str(
        messages[1]["content"]
    )


def test_a_long_run_of_calls_shows_its_shape_rather_than_every_call():
    messages = render_messages(
        build_groups([owner(1, "整理一下"), bubble(2, "整理完了", offset=99)]),
        tool_activity={
            "t1": [action("move_file", at=index, subject=f"f{index}") for index in range(9)]
        },
        action_limit=12,
    )
    body = text(messages[1])
    assert "[tool_call] move_file(f0) ×9 -> ok" in body


def test_a_turn_without_work_carries_no_action_line():
    messages = render_messages(
        build_groups([owner(1, "早"), bubble(2, "早")]), tool_activity={}
    )
    assert "[did:" not in text(messages[1])


def test_uncertain_delivery_stays_marked():
    messages = render_messages(
        build_groups([owner(1, "在吗"), bubble(2, "在", delivery_state="uncertain")])
    )
    assert "delivery uncertain" in text(messages[1])


def test_transcript_never_opens_on_an_assistant_reply():
    transcript = build_transcript(
        [
            owner(1, "第一轮"),
            bubble(2, "第一轮回复"),
            owner(3, "第二轮"),
            bubble(4, "第二轮回复"),
        ],
        max_groups=3,
    )
    assert [message["role"] for message in transcript.messages] == [
        "user",
        "assistant",
    ]
    assert [group.parts for group in transcript.orphaned] == [("第一轮回复",)]


def test_proactive_speech_without_an_owner_message_is_kept_as_evidence():
    transcript = build_transcript(
        [
            bubble(1, "我看到一条新闻", turn_id="hb1"),
            bubble(2, "有点想跟你说", turn_id="hb1"),
            bubble(3, "你还没睡吧", turn_id="hb2"),
        ]
    )
    assert transcript.messages == []
    assert len(transcript.orphaned) == 2

    evidence = render_delivered_bubble_evidence(
        transcript.orphaned,
        timezone=TEST_TIMEZONE,
    )
    assert "Momoi bubbles already delivered" in evidence
    assert "我看到一条新闻" in evidence
    assert "[owner did not reply" in evidence
    assert "你还没睡吧" in evidence


def test_owner_reply_after_proactive_speech_keeps_the_whole_exchange():
    transcript = build_transcript(
        [
            owner(1, "早", turn_id="t0"),
            bubble(2, "早", turn_id="t0"),
            bubble(3, "刚看到一条新闻", turn_id="hb1", offset=3600),
            owner(4, "什么新闻", turn_id="t1", offset=7200),
        ]
    )
    assert [message["role"] for message in transcript.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert text(transcript.messages[2]) == "[owner did not reply · 1h later]"
    assert transcript.orphaned == []


def test_selection_keeps_the_latest_exchange_under_a_tight_budget():
    groups = build_groups(
        [
            owner(1, "很久以前的一句话"),
            bubble(2, "很久以前的回复"),
            owner(3, "刚刚"),
        ]
    )
    selected = select_groups(groups, token_budget=1)
    assert [group.parts for group in selected] == [("刚刚",)]


def test_time_marker_appears_only_when_it_changes_meaning():
    messages = render_messages(
        build_groups(
            [
                owner(1, "早"),
                bubble(2, "早", offset=5),
                owner(3, "在忙吗", offset=2 * 3600),
            ]
        ),
        gap_seconds=1800,
    )
    assert text(messages[0]).startswith("[2026-08-31T20:00")
    assert text(messages[1]) == "早"
    assert text(messages[2]).startswith("[22:00]")


def test_a_new_day_shows_the_date_rather_than_only_a_clock_time():
    messages = render_messages(
        build_groups(
            [
                owner(1, "晚安"),
                bubble(2, "晚安", offset=5),
                owner(3, "早", offset=5 * 3600),
            ]
        ),
        gap_seconds=1800,
    )
    assert text(messages[2]).startswith("[2026-09-01T01:00")


def test_build_transcript_returns_protocol_messages_and_groups():
    transcript = build_transcript(
        [owner(1, "在吗"), bubble(2, "在")], max_groups=2
    )
    assert [message["role"] for message in transcript.messages] == [
        "user",
        "assistant",
    ]
    assert len(transcript.groups) == 2
    assert transcript.token_estimate > 0
