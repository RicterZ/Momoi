from zoneinfo import ZoneInfo

from momoi.models import IncomingMessage
from momoi.runtime.turn_support import owner_content_blocks


def test_each_owner_message_has_its_own_bubble_and_keeps_internal_newlines():
    events = [
        IncomingMessage('1', '1', '第一行\n第二行', 1, 1),
        IncomingMessage('2', '2', '另一条 </bubble> & 消息', 2, 2),
    ]
    blocks = owner_content_blocks(events, lambda _: [], ZoneInfo('UTC'), 'runtime')
    assert blocks == [{'type': 'text', 'text': (
        'runtime\n\n<current_messages>\n'
        '[1970-01-01T00:00:01+00:00] 第一行\n第二行\n'
        '[1970-01-01T00:00:02+00:00] 另一条 &lt;/bubble&gt; &amp; 消息\n'
        '</current_messages>'
    )}]


def test_owner_attachment_stays_inside_its_message_bubble():
    image = {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}}
    events = [
        IncomingMessage('1', '1', '看这张图', 1, 1, ({'image': True},)),
        IncomingMessage('2', '2', '然后看这句话', 2, 2),
    ]
    blocks = owner_content_blocks(events, lambda segments: [image] if segments else [], ZoneInfo('UTC'))
    assert '[1970-01-01T00:00:01+00:00] ' in blocks[0]['text']
    assert '看这张图' in blocks[0]['text']
    assert blocks[1] == image
    assert blocks[2] == {'type': 'text', 'text': (
        '\n[1970-01-01T00:00:02+00:00] 然后看这句话\n</current_messages>'
    )}
    assert len(blocks) == 3
