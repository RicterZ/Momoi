import asyncio
import hashlib
from unittest.mock import AsyncMock, call
from zoneinfo import ZoneInfo

from momoi.channel import SendRejected
from momoi.channel.napcat import NapCatChannel, NapCatConfig
from momoi.channel.napcat.favorites import FavoriteStickers
from momoi.models import AgentReply, IncomingMessage, MessageRecalled
from momoi.runtime.turn_support import owner_content_blocks
from momoi.storage.store import Store


def channel():
    return NapCatChannel(NapCatConfig('ws://localhost', '20000', 1, 60, 30, 30, 20))


def test_favorite_sync_uploads_to_napcat_and_reuses_qq_resource(tmp_path):
    async def scenario():
        path = tmp_path / 'synthetic.png'
        path.write_bytes(b'synthetic sticker')
        md5 = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
        favorite = {'md5': md5.upper(), 'url': 'https://example.invalid/qq-favorite'}
        request = AsyncMock(side_effect=[{'data': []}, {'data': {'file': '/napcat/tmp/sticker.png'}},
                                        {'data': {}}, {'data': [favorite]}, {'data': [favorite]}])
        catalog = [{'slug': 'synthetic', 'path': str(path)}]
        sync = FavoriteStickers(request, lambda: catalog)
        await sync.sync()
        sync.changed.clear()
        assert await sync.segment('synthetic') == {
            'type': 'image', 'data': {'file': favorite['url'], 'sub_type': 1}}
        await sync.sync()
        assert [c.args[0] for c in request.await_args_list] == [
            'fetch_custom_face_detail', 'download_file', 'add_custom_face',
            'fetch_custom_face_detail', 'fetch_custom_face_detail']
        assert request.await_args_list[2] == call('add_custom_face', {
            'file': '/napcat/tmp/sticker.png', 'is_origin': True})
        catalog.clear()
        await sync.sync()
        assert sync.resources == {}
        assert not any(c.args[0] == 'delete_custom_face' for c in request.await_args_list)
    asyncio.run(scenario())


def test_favorite_failure_does_not_send_ordinary_image(tmp_path):
    async def scenario():
        path = tmp_path / 'synthetic.png'
        path.write_bytes(b'synthetic')
        item = channel()
        request = AsyncMock(side_effect=SendRejected('unsupported'))
        item._request_action = request
        item.configure_emotions(lambda: [{'slug': 'synthetic', 'path': str(path)}])
        try:
            await item.send_message({'emotion_slug': 'synthetic', 'segments': [
                {'type': 'image', 'data': {'file': str(path)}}]})
        except SendRejected:
            pass
        else:
            raise AssertionError('Unsynchronized stickers must not silently become ordinary images')
        request.assert_awaited_once_with('fetch_custom_face_detail', {'count': 1000})
    asyncio.run(scenario())


def test_startup_and_catalog_change_trigger_sync():
    async def scenario():
        ready = asyncio.Event()
        sync = FavoriteStickers(AsyncMock(), lambda: [])
        completed = asyncio.Event()
        sync.sync = AsyncMock(side_effect=lambda: completed.set())
        worker = asyncio.create_task(sync.run(ready))
        try:
            await asyncio.sleep(0)
            sync.sync.assert_not_awaited()
            ready.set()
            await asyncio.wait_for(completed.wait(), 1)
            completed.clear()
            sync.notify()
            await asyncio.wait_for(completed.wait(), 1)
            assert sync.sync.await_count == 2
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_emotion_add_and_replace_notify_but_delete_does_not(tmp_path):
    store = Store(tmp_path / 'store.sqlite3', workspace=tmp_path)
    try:
        notifications = []
        store.emotions_changed = lambda: notifications.append(True)
        path = tmp_path / 'synthetic.png'
        path.write_bytes(b'synthetic')
        store.add_emotion('synthetic', path, 'test')
        store.add_emotion('synthetic', path, 'updated test')
        store.delete_emotion('synthetic')
        assert len(notifications) == 2
    finally:
        store.close()


def test_input_status_is_best_effort_and_uses_owner_qq():
    async def scenario():
        item = channel()
        item._ready.set()
        item._ws = type('Socket', (), {'closed': False})()
        item._request_action = AsyncMock(side_effect=[{}, SendRejected('unsupported')])
        await item.set_typing(True)
        await item.set_typing(False)
        assert item._request_action.await_args_list == [
            call('set_input_status', {'user_id': '20000', 'event_type': 1}),
            call('set_input_status', {'user_id': '20000', 'event_type': 0})]
    asyncio.run(scenario())


def test_only_owner_and_bot_recall_events_are_received():
    async def scenario():
        item = channel()
        receive = AsyncMock()
        base = {'post_type': 'notice', 'notice_type': 'friend_recall',
                'self_id': 30000, 'message_id': 123, 'time': 10}
        await item._handle_payload({**base, 'user_id': 40000}, receive)
        receive.assert_not_awaited()
        await item._handle_payload({**base, 'user_id': 20000}, receive)
        assert isinstance(receive.await_args.args[0], MessageRecalled)
        assert receive.await_args.args[0].author == 'owner'
        await item._handle_payload({**base, 'user_id': 30000}, receive)
        assert receive.await_args.args[0].author == 'assistant'
    asyncio.run(scenario())


def test_recall_is_durable_event_without_rewriting_owner_text(tmp_path):
    store = Store(tmp_path / 'store.sqlite3', workspace=tmp_path)
    try:
        original = IncomingMessage('original', '123', 'synthetic request', 1, 1, channel='napcat')
        store.add_event(original)
        store.commit_turn([original], original.text, AgentReply([]), turn_id='owner-turn')
        notice = MessageRecalled('recall:123', '123', 2, 'napcat')
        update = store.record_message_recall(notice)
        assert update is not None
        assert store.message_recalled('napcat', '123')
        assert store.record_message_recall(notice) is None
        rows = store.recent_conversation_messages(10, 10000)
        assert rows[0]['role'] == 'user' and rows[0]['content'] == 'synthetic request'
        assert rows[-1]['role'] == 'event'
        assert rows[-1]['event_source'] == 'napcat:message_recall'
        assert 'synthetic request' not in rows[-1]['content']
        assert store.pending_events() == []
        blocks = owner_content_blocks([update], lambda _: [], ZoneInfo('UTC'))
        text = ''.join(block['text'] for block in blocks)
        assert '<event source="napcat:message_recall">' in text
        assert '<message ' not in text
        assert store.turn_workflow_kind(notice.event_id) == 'channel_event'
    finally:
        store.close()


def test_recall_cancels_only_reply_to_that_message(tmp_path):
    store = Store(tmp_path / 'store.sqlite3', workspace=tmp_path)
    try:
        for num in (1, 2):
            event = IncomingMessage(f'event-{num}', str(num), 'synthetic', num, num, channel='napcat')
            store.add_event(event)
            store.commit_turn([event], event.text, AgentReply([f'reply-{num}']),
                              turn_id=f'turn-{num}', target_channel='napcat')
        assert store.cancel_pending_outbox('napcat', 'message_recalled', source_message_id='1') == 1
        rows = store._db.execute('SELECT text, state FROM outbox ORDER BY id').fetchall()
        assert [tuple(row) for row in rows] == [('reply-1', 'superseded'), ('reply-2', 'pending')]
    finally:
        store.close()


def test_delivery_receipt_identifies_only_bot_messages_on_target_channel(tmp_path):
    store = Store(tmp_path / 'store.sqlite3', workspace=tmp_path)
    try:
        store.commit_turn([], '', AgentReply(['synthetic reply']), turn_id='sent', target_channel='napcat')
        row = store.due_outbox()[0]
        store.mark_sending(row.id)
        store.record_delivery_receipt(row.id, '456')
        store.mark_sent(row.id)
        assert store.recallable_delivery('napcat') == (row.id, '456')
        assert store.recallable_delivery('napcat', row.id) == (row.id, '456')
        try:
            store.recallable_delivery('other', row.id)
        except ValueError:
            pass
        else:
            raise AssertionError('Cannot recall a message from another channel')
        notice = MessageRecalled('bot-recall', '456', 3, 'napcat', 'assistant')
        assert store.record_message_recall(notice) is not None
        assert not store.message_recalled('napcat', '456')
        assert store.message_recall_recorded('napcat', '456')
        assert store.record_message_recall(MessageRecalled('duplicate-bot-recall', '456', 4,
                                                         'napcat', 'assistant')) is None
    finally:
        store.close()


def test_old_database_migrates_channel_events_without_losing_turns(tmp_path):
    from momoi.storage.core import migrations
    import sqlite3
    db = sqlite3.connect(tmp_path / 'old.sqlite3')
    try:
        db.execute("""CREATE TABLE turns (
            id TEXT PRIMARY KEY, workflow_kind TEXT CHECK (workflow_kind IN ('owner', 'memory_maintenance')))
        """)
        db.execute("INSERT INTO turns VALUES ('existing', 'owner')")
        db.commit()
        with migrations.migration_transaction(db):
            migrations._add_channel_event_workflow(db)
        assert db.execute("SELECT * FROM turns WHERE id='existing'").fetchone() == ('existing', 'owner')
        db.execute("INSERT INTO turns VALUES ('recall', 'channel_event')")
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        db.close()
