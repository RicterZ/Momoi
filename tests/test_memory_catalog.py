from momoi.memory import Memory
from momoi.storage.memory.catalog import MOMOI_MEMORY_TAGS
from momoi.storage.memory.context import MEMORY_CATALOG_INLINE_LIMIT, memory_catalog_text


def seed(memory, key, tags, *, scope='', activation=None, content='无糖咖啡'):
    source = {'event_id': 'e', 'quote': content}
    return memory.repository.write(
        {'kind': 'preference', 'key': key, 'content': content,
         'activation': activation or ('scoped' if scope else 'recall'), 'expires_at': None,
         'meta': {'tags': tags, 'scope': scope}}, [], source, [source], now=100,
    )


def forget(memory, memory_id):
    memory.repository.forget(memory.snapshots([memory_id])[memory_id], {'event_id': 'e', 'quote': '忘掉'}, now=200)



def test_catalog_counts_recallable_memory_and_inlines_small_topics(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    leisure = [seed(memory, f'game {index}', ['leisure']) for index in range(MEMORY_CATALOG_INLINE_LIMIT + 1)]
    food = seed(memory, '不吃香菜', ['food_drink', 'health'])
    untagged = seed(memory, '称呼', [])
    seed(memory, 'always', ['food_drink'], activation='always')
    seed(memory, 'webhook only', ['food_drink'], scope='webhook')
    deleted = seed(memory, 'deleted', ['food_drink'])
    forget(memory, deleted)

    text = memory_catalog_text(memory.repository)
    assert f'leisure（{MOMOI_MEMORY_TAGS.tags["leisure"]}） {len(leisure)} 条' in text
    assert f'health（{MOMOI_MEMORY_TAGS.tags["health"]}）：#{food} 不吃香菜' in text
    assert text.splitlines()[-1] == f'untagged（未标主题）：#{untagged} 称呼'
    assert 'always' not in text and 'webhook only' not in text and f'#{deleted}' not in text
    assert memory_catalog_text(memory.repository) == text
    assert 'webhook only' in memory_catalog_text(memory.repository, scope='webhook')


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

