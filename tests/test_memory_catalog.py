from momoi.memory import Memory
from momoi.storage.memory.catalog import MOMOI_MEMORY_TAGS
from momoi.storage.memory.context import memory_catalog_text


def seed(memory, key, tags, *, scope='', activation=None, kind='preference', content='无糖咖啡'):
    source = {'event_id': 'e', 'quote': content}
    return memory.repository.write(
        {'kind': kind, 'key': key, 'content': content,
         'activation': activation or ('scoped' if scope else 'recall'), 'expires_at': None,
         'meta': {'tags': tags, 'scope': scope}}, [], source, [source], now=100,
    )


def forget(memory, memory_id):
    memory.repository.forget(memory.snapshots([memory_id])[memory_id], {'event_id': 'e', 'quote': '忘掉'}, now=200)


def test_catalog_counts_recallable_memory_by_topic(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    leisure = [seed(memory, f'game {index}', ['leisure']) for index in range(3)]
    seed(memory, '不吃香菜', ['food_drink', 'health'])
    seed(memory, '称呼', [])
    seed(memory, 'always', ['food_drink'], activation='always')
    seed(memory, 'webhook only', ['food_drink'], scope='webhook')
    deleted = seed(memory, 'deleted', ['food_drink'])
    forget(memory, deleted)

    text = memory_catalog_text(memory.repository)
    assert f'leisure（{MOMOI_MEMORY_TAGS.tags["leisure"]}） {len(leisure)} 条' in text
    assert f'health（{MOMOI_MEMORY_TAGS.tags["health"]}） 1 条' in text
    assert f'food_drink（{MOMOI_MEMORY_TAGS.tags["food_drink"]}） 1 条' in text
    assert text.splitlines()[-1] == 'untagged（未标主题） 1 条'
    assert '#' not in text and '不吃香菜' not in text
    assert memory_catalog_text(memory.repository) == text
    assert memory_catalog_text(memory.repository, scope='webhook').splitlines()[1:] == [
        f'food_drink（{MOMOI_MEMORY_TAGS.tags["food_drink"]}） 1 条'
    ]


def test_catalog_is_empty_without_recallable_memory(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    seed(memory, 'always', ['food_drink'], activation='always')
    assert memory_catalog_text(memory.repository) == ''


def test_catalog_sits_in_context_prefix_after_goal_directory():
    from momoi.runtime.turn_support import context_data_message
    message = context_data_message(
        ('long_term_memories', 'a'), ('goal_directory', 'g'), ('memory_catalog', '#1 <x>'), required=True,
    )
    text = message['content'] if isinstance(message['content'], str) else message['content'][0]['text']
    assert text.index('<goal_directory>') < text.index('<memory_catalog>')
    assert '#1 &lt;x&gt;' in text
