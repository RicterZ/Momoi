from momoi.runtime.parsing import parse_tagged_bubbles
from momoi.runtime.transcript.rendering import render_bubble


def test_reads_logged_transcript_boundaries_and_emotion():
    text = '<bubble turn="T-52">\n微博登录过期啦…\n</bubble>\n<bubble turn="T-52">\nemotion://awkward-sweat\n</bubble>\n<bubble turn="T-52">\n第一行\n第二行\n</bubble>'
    assert parse_tagged_bubbles(text) == ['微博登录过期啦…', 'emotion://awkward-sweat', '第一行\n第二行']


def test_decodes_transcript_escaping_once():
    text = 'a < b & c\n字面标签 <bubble> 与 &lt;'
    for turn in ('', 'T-21', 'T-"<&'):
        for delivery_state in ('delivered', 'queued'):
            assert parse_tagged_bubbles(
                render_bubble(text, turn=turn, delivery_state=delivery_state)
            ) == [text]


def test_incomplete_or_example_text_is_not_partially_sent():
    for text in (
        '', '普通正文', '[turn=T52]', '<bubble></bubble>', '<bubble>未结束',
        '<bubble>第一条</bubble><bubble>未结束',
        '<bubble><bubble>嵌套</bubble></bubble>',
        '<bubble turn=T-21>无引号</bubble>',
        '<bubble turn="T-21>未闭合属性</bubble>',
        '<bubble turn="T-21">第一条</bubble><bubble turn="T-22">未结束',
        '<bubble turn="T-21"><bubble turn="T-22">嵌套</bubble></bubble>',
        '```xml\n<bubble>示例</bubble>\n```',
    ):
        assert parse_tagged_bubbles(text) is None


def test_extracts_only_explicit_bubbles_outside_examples():
    for text in (
        '<bubble>好了</bubble>剩余正文',
        '我准备发送：<bubble>好了</bubble>',
        '```xml\n<bubble>示例</bubble>\n```\n<bubble>好了</bubble>',
        '~~~xml\n<bubble>示例</bubble>\n~~~\n<bubble>好了</bubble>',
    ):
        assert parse_tagged_bubbles(text) == ['好了']


def test_preserves_order_and_single_newlines_in_mixed_text():
    assert parse_tagged_bubbles(
        '刷到一条消息\n<bubble>第一行\n第二行</bubble>私有说明'
        '<bubble>emotion://happy-dance</bubble>'
    ) == ['第一行\n第二行', 'emotion://happy-dance']
