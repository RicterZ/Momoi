import pytest

from momoi.runtime.parsing import parse_tagged_bubbles
from momoi.runtime.transcript.rendering import render_bubble


def test_reads_logged_transcript_boundaries_and_emotion():
    text = '[turn=T52]\n<bubble>\n微博登录过期啦…\n</bubble>\n<bubble>\nemotion://awkward-sweat\n</bubble>\n<bubble>\n第一行\n第二行\n</bubble>'
    assert parse_tagged_bubbles(text) == ['微博登录过期啦…', 'emotion://awkward-sweat', '第一行\n第二行']


def test_decodes_transcript_escaping_once():
    text = 'a < b & c\n字面标签 <bubble> 与 &lt;'
    assert parse_tagged_bubbles(render_bubble(text)) == [text]


@pytest.mark.parametrize('text', [
    '', '普通正文', '[turn=T52]', '<bubble></bubble>',
    '<bubble>未结束', '<bubble>好了</bubble>剩余正文',
    '我准备发送：<bubble>好了</bubble>',
    '<bubble>第一条</bubble><bubble>未结束',
    '<bubble><bubble>嵌套</bubble></bubble>',
    '<bubble>第一条</bubble>[turn=T53]<bubble>第二条</bubble>',
    '```xml\n<bubble>示例</bubble>\n```',
])
def test_incomplete_or_mixed_text_is_not_partially_sent(text):
    assert parse_tagged_bubbles(text) is None
