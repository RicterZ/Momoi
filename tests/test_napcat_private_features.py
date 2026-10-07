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
        assert '用户撤回了消息' in rows[-1]['content']
        assert '老师' not in rows[-1]['content']
        assert '无需回复' not in rows[-1]['content']
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


def test_private_poke_filters_group_and_unrelated_participants_and_keeps_direction():
    from momoi.models import MessagePoked
    async def scenario():
        item = channel()
        receive = AsyncMock()
        base = {'post_type': 'notice', 'notice_type': 'notify', 'sub_type': 'poke',
                'self_id': 30000, 'user_id': 20000, 'sender_id': 20000, 'target_id': 30000, 'time': 10}
        for extra in ({'group_id': 50000}, {'sender_id': 40000}, {'target_id': 40000}):
            await item._handle_payload({**base, **extra}, receive)
        receive.assert_not_awaited()
        await item._handle_payload(base, receive)
        first = receive.await_args.args[0]
        assert isinstance(first, MessagePoked)
        assert (first.author, first.target) == ('owner', 'assistant')
        await item._handle_payload(base, receive)
        assert receive.await_args.args[0].event_id != first.event_id
        # user_id is the private-chat peer, not the actual sender.
        await item._handle_payload({**base, 'sender_id': 30000, 'target_id': 20000}, receive)
        echo = receive.await_args.args[0]
        assert (echo.author, echo.target) == ('assistant', 'owner')
    asyncio.run(scenario())


def test_poke_is_archived_and_rendered_as_event_without_forcing_a_reply(tmp_path):
    from momoi.models import MessagePoked
    store = Store(tmp_path / 'store.sqlite3', workspace=tmp_path)
    try:
        notice = MessagePoked('poke-1', 3, 'napcat', 'owner', 'assistant')
        update = store.record_message_poke(notice)
        assert update.text == '【QQ 戳一戳】\n用户戳了机器人一下。'
        assert store.record_message_poke(notice) is None
        blocks = owner_content_blocks([update], lambda _: [], ZoneInfo('UTC'))
        text = ''.join(b['text'] for b in blocks)
        assert '<event source="napcat:poke">' in text
        assert '<message ' not in text
        rows = store.recent_conversation_messages(10, 10000)
        assert len(rows) == 1 and rows[0]['role'] == 'event'
        assert rows[0]['event_source'] == 'napcat:poke'
        assert store.pending_events() == []
        self_poke = store.record_message_poke(MessagePoked('poke-2', 4, 'napcat', 'owner', 'owner'))
        assert self_poke.text == '【QQ 戳一戳】\n用户戳了自己一下。'
    finally:
        store.close()


def test_active_poke_calls_private_api_with_configured_owner_only():
    async def scenario():
        item = channel()
        item._request_action = AsyncMock(return_value={'status': 'ok', 'retcode': 0})
        await item.poke_owner()
        item._request_action.assert_awaited_once_with('friend_poke', {'user_id': '20000'})
    asyncio.run(scenario())


def test_received_file_downloads_to_workspace_and_renders_local_path(tmp_path):
    from aiohttp import web, ClientSession
    from pathlib import Path
    from momoi.channel.napcat import load_config, render_segments

    async def scenario():
        app = web.Application()
        async def asset(request):
            return web.Response(body=b'synthetic text')
        app.router.add_get('/asset', asset)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            config = load_config({'url': 'ws://localhost', 'owner_qq': '20000'}, tmp_path)
            item = NapCatChannel(config)
            item._request_action = AsyncMock(return_value={'data': {'url': f'http://127.0.0.1:{port}/asset'}})
            async with ClientSession() as session:
                item._session = session
                segments = await item._enrich_segments(({'type': 'file', 'data': {
                    'file': '../../sample.txt', 'file_id': 'synthetic-id'}},))
                path = Path(segments[0]['data']['file'])
                assert path.read_bytes() == b'synthetic text'
                assert path.name == 'sample.txt'
                assert path.is_relative_to(tmp_path / 'channel/napcat/files')
                assert str(path) in render_segments(segments)
                item._request_action.assert_awaited_once_with('get_private_file_url', {'file_id': 'synthetic-id'})
                object.__setattr__(config, 'media_max_bytes', 2)
                failed = await item._enrich_segments(({'type': 'file', 'data': {
                    'file': 'large.txt', 'url': f'http://127.0.0.1:{port}/asset'}},))
                assert 'source=unavailable' in render_segments(failed)
                assert len(list((tmp_path / 'channel/napcat/files').glob('*/*'))) == 1
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
