from zoneinfo import ZoneInfo
from xml.etree import ElementTree

from momoi.models import IncomingMessage
from momoi.runtime.turn_support import owner_content_blocks


def test_each_owner_message_has_its_own_bubble_and_keeps_internal_newlines():
    events = [
        IncomingMessage('1', '1', '第一行\n第二行', 1, 1),
        IncomingMessage('2', '2', '另一条 </bubble> & 消息', 2, 2),
    ]
    blocks = owner_content_blocks(events, lambda _: [], ZoneInfo('UTC'), 'runtime')
    text = ''.join(block['text'] for block in blocks)
    root = ElementTree.fromstring(text.removeprefix('runtime\n\n'))
    bubbles = root.findall('bubble')
    assert [bubble.attrib for bubble in bubbles] == [
        {'time': '1970-01-01T00:00:01+00:00'},
        {'time': '1970-01-01T00:00:02+00:00'},
    ]
    assert [bubble.text for bubble in bubbles] == [
        '\n第一行\n第二行\n', '\n另一条 </bubble> & 消息\n',
    ]


def test_owner_attachment_stays_inside_its_message_bubble():
    image = {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}}
    events = [
        IncomingMessage('1', '1', '看这张图', 1, 1, ({'image': True},)),
        IncomingMessage('2', '2', '然后看这句话', 2, 2),
    ]
    blocks = owner_content_blocks(events, lambda segments: [image] if segments else [], ZoneInfo('UTC'))
    assert '<bubble time="1970-01-01T00:00:01+00:00">\n' in blocks[0]['text']
    assert '看这张图' in blocks[0]['text']
    assert blocks[1] == image
    assert blocks[2]['text'] == '\n</bubble>\n'
    assert blocks[3]['text'].startswith('<bubble time="1970-01-01T00:00:02+00:00">\n')
    assert '然后看这句话' in blocks[3]['text']
