import time
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import pytest

from momoi.runtime.transcript.building import (
    build_groups,
    build_transcript as _build_transcript,
    select_groups,
)
from momoi.runtime.transcript.rendering import (
    owner_idle_gap_message,
    render_messages as _render_messages,
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

    assert labels == {"turn-a": "T-1", "turn-b": "T-2"}
    for message, label in zip(messages, ["T-1", "T-1", "T-2"], strict=True):
        if message['role'] == 'user':
            assert text(message).startswith(f'[message][2026-08-31T20:00:00+08:00][user][turn:{label}] ')
            continue
        document = ElementTree.fromstring(f"<history>{text(message)}</history>")
        assert document.find("bubble").attrib == {
            "time": "2026-08-31T20:00:00+08:00",
            "turn": label,
            **({"from": "user"} if message["role"] == "user" else {}),
        }
        assert "turn=" not in (document.text or "")


def test_owner_idle_gap_uses_last_owner_message_across_runtime_records():
    rows = [
        owner(1, "去吃饭", turn_id="owner", offset=0),
        {"id": 2, "turn_id": "webhook", "role": "event", "content": "天气", "created_at": BASE + 3600},
        {"id": 3, "turn_id": "heartbeat", "role": "heartbeat", "content": "工作记录", "created_at": BASE + 5400},
    ]
    message = owner_idle_gap_message(
        rows, now=BASE + 3600, timezone=TEST_TIMEZONE
    )
    assert message is not None
    assert message["role"] == "user"
    assert "2026-08-31T20:00:00+08:00" in text(message)
    assert "1h" in text(message)


def test_owner_idle_gap_is_transient_and_below_threshold_is_omitted():
    rows = [owner(1, "刚说完", offset=0)]
    assert owner_idle_gap_message(
        rows, now=BASE + 29 * 60, timezone=TEST_TIMEZONE
    ) is None


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


@pytest.mark.parametrize("row", [owner, bubble])
def test_bubble_boundaries_preserve_internal_newlines_and_literal_markup(row):
    parts = ["地址：上海\n电话：138", "正文含 </bubble><bubble> & 符号"]
    messages = render_messages(
        build_groups([row(index, part) for index, part in enumerate(parts, 1)])
    )
    if row is owner:
        from xml.sax.saxutils import escape
        for part in parts:
            assert escape(part) in text(messages[0])
        assert text(messages[0]).count('[message]') == 2
        split = render_messages(build_groups([row(1, "地址：上海"), row(2, "电话：138")]))
        assert text(split[0]).count('[message]') == 2
        assert '地址：上海' in text(split[0]) and '电话：138' in text(split[0])
        return
    document = ElementTree.fromstring(f"<history>{text(messages[0])}</history>")
    assert [item.text for item in document.findall("bubble")] == [
        f"\n{part}\n" for part in parts
    ]

    split_messages = render_messages(
        build_groups([row(1, "地址：上海"), row(2, "电话：138")])
    )
    split_document = ElementTree.fromstring(
        f"<history>{text(split_messages[0])}</history>"
    )
    assert [item.text for item in split_document.findall("bubble")] == [
        "\n地址：上海\n", "\n电话：138\n"
    ]


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
    messages = render_messages(groups)
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert text(messages[2]) == "[owner did not reply · 8h later]"
    document = ElementTree.fromstring(f"<history>{text(messages[3])}</history>")
    assert document.find("bubble").attrib["time"] == "2026-09-01T04:00:00+08:00"


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
            bubble(5, "被新消息打断", delivery_state="superseded"),
            bubble(4, "早上好"),
        ]
    )
    assert len(groups) == 2
    assert groups[1].parts == ("早上好",)


def test_inbound_events_are_not_owner_speech():
    event = owner(1, "webhook payload") | {"role": "event"}
    groups = build_groups([event, owner(2, "看到了")])
    assert [group.role for group in groups] == ["event", "user"]


def event(identifier, content, *, offset=0, source="webhook:event-message"):
    return owner(identifier, content, turn_id=f"event-{identifier}", offset=offset) | {
        "role": "event", "event_source": source,
    }


def test_event_has_stable_identity_source_time_and_escaped_content():
    row = event(71, '包裹到了\n原文包含 </event> & 符号', source='webhook:门"锁')
    full = build_transcript([owner(1, "早"), bubble(2, "早"), row])
    short = build_transcript([row])
    assert text(full.messages[-1]) == text(short.messages[0])
    document = ElementTree.fromstring(text(short.messages[0]))
    assert document.tag == "event"
    assert document.attrib == {
        "id": "E71", "source": 'webhook:门"锁',
        "received_at": "2026-08-31T20:00:00+08:00",
    }
    assert document.text == '\n包裹到了\n原文包含 </event> & 符号\n'


def test_event_reception_order_is_independent_of_reply_archival_order():
    transcript = build_transcript([
        owner(1, "出门了"),
        event(2, "包裹到了", offset=20),
        bubble(3, "路上小心", offset=10),
        event(4, "另一件也到了", offset=20),
        bubble(5, "收到到货通知", turn_id="heartbeat", offset=30),
    ])
    assert [group.message_ids for group in transcript.groups] == [
        (1,), (3,), (2,), (4,), (5,),
    ]
    assert "did not reply" not in str(transcript.messages)
    assert "without replying" not in str(transcript.messages)


def test_event_between_owner_bubbles_does_not_invent_an_assistant_reply():
    transcript = build_transcript([
        owner(1, "出门了"), event(2, "包裹到了", offset=10),
        owner(3, "到公司了", turn_id="next", offset=20),
    ])
    assert [message["role"] for message in transcript.messages] == ["user"] * 3
    assert '<event id="E2"' in text(transcript.messages[1])
    assert "<bubble>" not in text(transcript.messages[1])


def test_event_splitting_a_turn_does_not_duplicate_its_tool_activity():
    transcript = build_transcript([
        owner(1, "查一下"), bubble(2, "开始查了", offset=10),
        event(3, "包裹到了", offset=20), bubble(4, "查好了", offset=30),
    ], tool_activity={"t1": [action("first", at=15), action("second", at=25)]})
    rendered = str(transcript.messages)
    assert rendered.count("first()") == 1
    assert rendered.count("second()") == 1
    assert rendered.index("first()") < rendered.index('<event id="E3"')
    assert rendered.index('<event id="E3"') < rendered.index("second()")


def test_native_replay_does_not_move_actions_across_an_interleaved_event():
    groups = build_groups([
        owner(1, "查一下"), bubble(2, "开始查了", offset=10),
        event(3, "包裹到了", offset=20), bubble(4, "查好了", offset=30),
    ])
    messages = render_messages(
        groups, tool_activity={"t1": [action("first", at=15), action("second", at=25)]},
        native_exchanges={"t1": [{
            "content": [{"type": "text", "text": "不能提前重放的后续判断"}],
            "results": [],
        }]},
    )
    rendered = str(messages)
    assert "不能提前重放" not in rendered
    assert rendered.index("first()") < rendered.index('<event id="E3"')
    assert rendered.index('<event id="E3"') < rendered.index("second()")


@pytest.mark.parametrize("kind,prefix", [("goal", "G"), ("heartbeat", "H")])
def test_review_follows_committed_speech_without_claiming_delivery(kind, prefix):
    review = {
        **owner(4, "Goal: 检查\nStatus: done\nLatest result: <已提交> & 等待投递", offset=30),
        "role": kind, "delivery_state": "internal",
    }
    transcript = build_transcript([
        owner(1, "帮我检查"),
        bubble(2, "检查完成", offset=20, delivery_state="queued"),
        review,
        bubble(5, "不应泄露的内部记录", offset=40, delivery_state="internal"),
    ])
    assert [group.role for group in transcript.groups] == ["user", "assistant", kind]
    assert 'delivery="queued"' in text(transcript.messages[1])
    goal = ElementTree.fromstring(text(transcript.messages[2]))
    assert goal.tag == kind
    assert goal.attrib == {"id": f"{prefix}4", "completed_at": "2026-08-31T20:00:30+08:00"}
    assert "<已提交> & 等待投递" in goal.text
    assert "不应泄露" not in str(transcript.messages)
    assert "did not reply" not in str(transcript.messages)


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


@pytest.mark.parametrize("label", ["", "T-21"])
def test_work_is_interleaved_with_the_words_that_narrate_it(label):
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
        labels={"t1": label},
    )
    def opening(offset):
        turn = f' turn="{label}"' if label else ""
        return f'<bubble time="2026-08-31T20:00:0{offset}+08:00"{turn}>'

    assert text(messages[1]).split("\n") == [
        opening(1),
        "好的，我刷微博",
        "</bubble>",
        "[tool_call] weibo_feed(home) -> ok",
        opening(3),
        "我发现了内容XXX",
        "</bubble>",
        "[tool_call] weibo_detail(4012) -> ok · ref=tr-9",
        opening(5),
        "刷完了",
        "</bubble>",
    ]


def test_a_failed_call_cannot_hide_behind_a_confident_reply():
    messages = render_messages(
        build_groups([owner(1, "查一下"), bubble(2, "查好了", offset=2)]),
        tool_activity={"t1": [action("curl", at=1, subject="http://x", ok=False)]},
    )
    assert "[tool_call] curl(http://x) -> failed: timed out" in str(
        messages[1]["content"]
    )


def test_native_exchange_replays_assistant_text_and_send_tool_once():
    sent = "我有话说"
    groups = build_groups([owner(1, "讲吧"), bubble(2, sent)])
    messages = render_messages(groups, native_exchanges={"t1": [{
        "content": [
            {"type": "text", "text": "先回应他。"},
            {"type": "tool_use", "id": "send-1", "name": "send_bubbles",
             "input": {"bubbles": [sent]}},
        ],
        "results": [{"type": "tool_result", "tool_use_id": "send-1",
                     "content": '{"ok":true,"state":"committed"}'}],
    }]})
    assert [item["role"] for item in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"][0]["text"] == "先回应他。"
    assert messages[1]["content"][1]["name"] == "send_bubbles"
    assert "committed" in str(messages[2])
    assert "message delivery confirmation" not in str(messages)
    assert sum(sent in str(item) for item in messages) == 1


def test_native_text_without_tool_is_private_and_followed_by_runtime_notice():
    groups = build_groups([owner(1, "讲吧"), bubble(2, "真正送出的内容")])
    messages = render_messages(groups, native_exchanges={"t1": [
        {"content": [{"type": "text", "text": "未发送的草稿"}],
         "results": [{"type": "text", "text": "[No action executed; no message sent.]"}]},
        {"content": [{"type": "tool_use", "id": "s1", "name": "send_bubbles",
                      "input": {"bubbles": ["真正送出的内容"]}}],
         "results": [{"type": "tool_result", "tool_use_id": "s1",
                      "content": '{"ok":true}'}]},
    ]})
    assert messages[1]["role"] == "assistant"
    assert "未发送的草稿" in str(messages[1])
    assert "no message sent" in str(messages[2])
    assert "真正送出的内容" in str(messages[3])
    assert "未发送的草稿" not in str(messages[4:])


def test_native_exchange_is_replayed_after_silent_owner_turn():
    groups = build_groups([
        owner(1, "第一条", turn_id="t1"),
        owner(2, "第二条", turn_id="t2"),
    ])
    messages = render_messages(groups, native_exchanges={"t1": [{
        "content": [{"type": "text", "text": "先查资料"},
                    {"type": "tool_use", "id": "r1", "name": "read_file",
                     "input": {"path": "notes.md"}}],
        "results": [{"type": "tool_result", "tool_use_id": "r1",
                     "content": '{"ok":true}'}],
    }]})
    assert [message["role"] for message in messages] == [
        "user", "assistant", "user", "user",
    ]
    assert "先查资料" in str(messages[1])
    assert "read_file" in str(messages[1])
    assert "ended the Turn without replying" not in str(messages)


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


@pytest.mark.parametrize("with_tools", [False, True])
def test_queued_bubbles_keep_boundaries_and_individual_delivery_state(with_tools):
    messages = render_messages(
        build_groups([
            owner(1, "提醒我"),
            bubble(2, "两点了", offset=1),
            bubble(3, "起来走走\n喝口水 & 休息一下", offset=3, delivery_state="queued"),
        ]),
        tool_activity={"t1": [action("clock", at=2)]} if with_tools else {},
    )
    document = ElementTree.fromstring(f"<history>{text(messages[1])}</history>")
    bubbles = document.findall("bubble")
    assert [item.attrib for item in bubbles] == [
        {"time": "2026-08-31T20:00:01+08:00"},
        {
            "time": "2026-08-31T20:00:03+08:00",
            "delivery": "queued",
        },
    ]
    assert [item.text for item in bubbles] == [
        "\n两点了\n", "\n起来走走\n喝口水 & 休息一下\n",
    ]
    if with_tools:
        assert text(messages[1]).index("clock()") < text(messages[1]).index('delivery="queued"')


def test_pending_proactive_speech_is_evidence_without_claiming_owner_silence():
    transcript = build_transcript([
        bubble(1, "喝水啦", turn_id="goal1", delivery_state="queued"),
        bubble(2, "外卖到了", turn_id="goal2", offset=10, delivery_state="queued"),
    ])
    evidence = "\n".join(text(message) for message in render_messages(transcript.orphaned))
    assert evidence.count('delivery="queued"') == 2
    assert evidence.count('<bubble time="') == 2
    assert "喝水啦" in evidence and "外卖到了" in evidence
    assert "still being delivered" in evidence
    assert "owner did not reply" not in evidence


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

    messages = render_messages(transcript.orphaned)
    evidence = "\n".join(text(message) for message in messages)
    assert messages[0]["role"] == "assistant"
    assert (
        '<bubble time="2026-08-31T20:00:00+08:00">\n'
        '我看到一条新闻\n</bubble>'
    ) in evidence
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


def test_each_bubble_carries_its_timestamp_without_a_separate_marker():
    messages = render_messages(
        build_groups(
            [
                owner(1, "早"),
                bubble(2, "早", offset=5),
                owner(3, "在忙吗", offset=2 * 3600),
            ]
        )
    )
    assert text(messages[0]).startswith('[message][2026-08-31T20:00:00+08:00][user] ')
    assert ElementTree.fromstring(f'<history>{text(messages[1])}</history>').find('bubble').attrib['time'] == '2026-08-31T20:00:05+08:00'
    assert text(messages[2]).startswith('[message][2026-08-31T22:00:00+08:00][user] ')


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


@pytest.mark.parametrize('kind', ['heartbeat', 'goal', 'plan_step'])
@pytest.mark.parametrize('speaks', [False, True])
def test_autonomous_completion_follows_native_actions(kind, speaks):
    rows = []
    if speaks:
        rows.append(bubble(1, '已检查', turn_id='auto', offset=5))
    rows.append(dict(id=2, turn_id='auto', role=kind, content='检查完成', created_at=BASE + 10))
    raw = '{"ok":true,"state":"completed","provenance":{"source":"runtime"}}'
    exchanges = {'auto': [{
        'content': [{'type': 'text', 'text': '开始检查'},
                    {'type': 'tool_use', 'id': 'check', 'name': 'exec', 'input': {'command': 'check'}}],
        'results': [{'type': 'tool_result', 'tool_use_id': 'check', 'content': raw}],
    }]}
    messages = render_messages(build_groups(rows), native_exchanges=exchanges)
    assert '开始检查' in str(messages[0])
    assert 'completed' in messages[1]['content'][0]['content']
    assert messages[1]['content'][0]['tool_use_id'] == 'check'
    assert '检查完成' in str(messages[2])
    assert len(messages) == 3
    assert 'message delivery confirmation' not in str(messages)


def test_webhook_trigger_stays_before_native_actions():
    rows = [dict(id=1, turn_id='auto', role='event', content='门开了',
                 event_source='webhook:door', created_at=BASE)]
    exchanges = {'auto': [{'content': [{'type': 'text', 'text': '检查门锁'}], 'results': []}]}
    messages = render_messages(build_groups(rows), native_exchanges=exchanges)
    assert '门开了' in str(messages[0])
    assert '检查门锁' in str(messages[1])
