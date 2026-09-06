from zoneinfo import ZoneInfo

from momoi.models import IncomingMessage
from momoi.runtime.turn_support import owner_content_blocks


def test_each_owner_message_has_its_own_bubble_and_keeps_internal_newlines():
    events = [
        IncomingMessage('1', '1', '第一行\n第二行', 1, 1),
        IncomingMessage('2', '2', '另一条 </bubble> & 消息', 2, 2),
    ]
    blocks = owner_content_blocks(events, lambda _: [], ZoneInfo('UTC'), 'runtime')
    text = ''.join(block['text'] for block in blocks)
    assert text.count('<bubble>') == 2
    assert text.count('</bubble>') == 2
    assert '第一行\n第二行\n</bubble>' in text
    assert '另一条 &lt;/bubble&gt; &amp; 消息\n</bubble>' in text
    assert text.startswith('runtime\n\n<current_owner_bubbles>\n<bubble>\n')
    assert text.endswith('</bubble>\n</current_owner_bubbles>')


def test_owner_attachment_stays_inside_its_message_bubble():
    image = {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}}
    events = [
        IncomingMessage('1', '1', '看这张图', 1, 1, ({'image': True},)),
        IncomingMessage('2', '2', '然后看这句话', 2, 2),
    ]
    blocks = owner_content_blocks(events, lambda segments: [image] if segments else [], ZoneInfo('UTC'))
    assert '<bubble>\n' in blocks[0]['text']
    assert '看这张图' in blocks[0]['text']
    assert blocks[1] == image
    assert blocks[2]['text'] == '\n</bubble>\n'
    assert blocks[3]['text'].startswith('<bubble>\n')
    assert '然后看这句话' in blocks[3]['text']
