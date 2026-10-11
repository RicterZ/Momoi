import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
from momoi.channel.dashboard import DashboardChannel
from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap
from momoi.dashboard.app import create_dashboard_app
from momoi.dashboard.auth import issue_dashboard_jwt
from momoi.dashboard.settings import DashboardSettings
from momoi.models import IncomingMessage
from momoi.storage import Store


class DashboardChatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'config.json'
        bootstrap(path)
        config = ConfigurationManager(path).dashboard_config()
        self.store = Store(config.database, config.workspace)
        self.addCleanup(self.store.close)
        self.received = []
        async def receive(event):
            self.store.add_event(event)
            self.received.append(event)
        self.runtime = SimpleNamespace(lock=asyncio.Lock(), suspended=False,
            daemon=SimpleNamespace(channels={'dashboard': DashboardChannel()}, _receive=receive))
        self.status = {'runtime_active': True, 'budget': {'blocked': False}}
        self.runtime.status = lambda: self.status
        self.client = TestClient(TestServer(create_dashboard_app(self.store, token=config.dashboard.token,
            settings=DashboardSettings.from_config(config), runtime=self.runtime)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.auth = 'Bearer ' + issue_dashboard_jwt(config.dashboard.token)

    async def test_auth_idempotence_and_budget_pause(self):
        payload = {'text': '老师来啦', 'id': 'message_001'}
        self.assertEqual((await self.client.post('/api/chat/messages', json=payload)).status, 401)
        self.assertEqual((await self.client.get('/api/chat/messages')).status, 401)
        self.client.session.headers['Authorization'] = self.auth
        self.assertEqual((await self.client.post('/api/chat/messages', json=payload)).status, 202)
        self.assertEqual((await self.client.post('/api/chat/messages', json=payload)).status, 200)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].channel, 'dashboard')
        self.assertEqual((await self.client.post('/api/chat/messages', json={**payload, 'text': 'changed'})).status, 409)
        self.status['budget']['blocked'] = True
        self.assertEqual((await self.client.post('/api/chat/messages', json={**payload, 'id': 'message_002'})).status, 409)
        self.assertEqual(len(self.received), 1)
        data = await (await self.client.get('/api/chat/messages')).json()
        self.assertEqual(data['messages'][0]['content'], payload['text'])
        self.assertTrue(data['runtime']['budget']['blocked'])

    async def test_validation_history_isolation_and_equal_timestamp_pagination(self):
        self.client.session.headers['Authorization'] = self.auth
        for payload in ([], {'text': '', 'id': 'valid_id'}, {'text': 'x' * 10001, 'id': 'valid_id'}, {'text': 'hi', 'id': '../bad'}):
            self.assertEqual((await self.client.post('/api/chat/messages', json=payload)).status, 400)
        now = time.time()
        for channel in ('dashboard', 'napcat'):
            for index in range(3):
                self.store.add_event(IncomingMessage(f'{channel}:{index}', str(index), f'{channel} {index}', now, now, channel=channel))
        first = await (await self.client.get('/api/chat/messages?limit=2')).json()
        self.assertTrue(first['has_more'])
        self.assertEqual(len(first['messages']), 2)
        cursor = first['messages'][0]
        second = await (await self.client.get('/api/chat/messages', params={'limit': 2, 'before': cursor['created_at'], 'before_id': cursor['id']})).json()
        self.assertFalse(second['has_more'])
        self.assertEqual(len(second['messages']), 1)
        self.assertEqual(len({row['id'] for row in first['messages'] + second['messages']}), 3)
        self.assertTrue(all(row['content'].startswith('dashboard') for row in first['messages'] + second['messages']))
        self.assertEqual((await self.client.get('/api/chat/messages?before=nan')).status, 400)

    async def test_history_ignores_malformed_legacy_event_payloads(self):
        self.client.session.headers['Authorization'] = self.auth
        now = time.time()
        for index, payload in enumerate(("", "not-json", '{"channel":')):
            event_id = f'legacy:{index}'
            self.store.add_event(IncomingMessage(event_id, str(index), 'legacy', now, now, channel='napcat'))
            self.store._db.execute('UPDATE events SET payload_json=? WHERE id=?', (payload, event_id))
        self.store._db.commit()
        response = await self.client.get('/api/chat/messages')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['messages'], [])
        self.assertEqual((await self.client.post('/api/chat/messages', json={'text': '你好', 'id': 'valid_msg_01'})).status, 202)
        response = await self.client.get('/api/chat/messages')
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual([row['content'] for row in data['messages']], ['你好'])
        self.assertTrue(data['typing'])

    async def test_live_reply_delivery_and_media_access(self):
        self.client.session.headers['Authorization'] = self.auth
        now = time.time()
        event = IncomingMessage('dashboard:replytest', 'replytest', '你好', now, now, channel='dashboard')
        self.store.add_event(event)
        self.store.begin_turn('chat-turn', 'owner', [event.event_id])
        self.store.queue_progress('chat-turn', 'reply', ['老师！'], 'dashboard')
        first = await (await self.client.get('/api/chat/messages')).json()
        self.assertEqual(first['messages'][-1]['content'], '老师！')
        self.assertEqual(first['messages'][-1]['delivery_state'], 'queued')
        self.assertTrue(first['typing'])
        self.runtime.daemon._active_turn_channel = 'dashboard'
        self.runtime.daemon._active_turn = object()
        row = self.store.due_outbox()[0]
        await self.runtime.daemon.channels['dashboard'].send_message(row.payload)
        self.store.mark_sent(row.id)
        second = await (await self.client.get('/api/chat/messages')).json()
        self.assertEqual(second['messages'][-1]['delivery_state'], 'delivered')
        self.assertEqual(second['messages'][-1]['id'], first['messages'][-1]['id'])
        self.assertFalse(second['typing'], 'Planner cleanup must not appear as typing after delivery')
        await self.runtime.daemon.channels['dashboard'].set_typing(True)
        self.assertTrue((await (await self.client.get('/api/chat/messages')).json())['typing'])
        await self.runtime.daemon.channels['dashboard'].set_typing(False)
        self.store.add_event(IncomingMessage('dashboard:interjection', 'interjection', '还有一句', time.time(), time.time(), channel='dashboard'))
        self.assertTrue((await (await self.client.get('/api/chat/messages')).json())['typing'])
        media = self.store._workspace / 'artifacts' / 'image.png'
        media.parent.mkdir(exist_ok=True)
        media.write_bytes(b'example-image')
        self.store.queue_progress('chat-turn', 'image', [{'action': 'message', 'segments': [{'type': 'image', 'data': {'file': str(media)}}]}], 'dashboard')
        media_row = self.store.due_outbox()[0]
        response = await self.client.get(f'/api/chat/media/{media_row.id}')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'example-image')
        emotion = self.store._workspace / 'emotion' / 'test.gif'
        emotion.parent.mkdir(exist_ok=True)
        emotion.write_bytes(b'GIF89a-test')
        with self.store._db:
            self.store._db.execute('UPDATE outbox SET media_path=? WHERE id=?', ('emotion/test.gif', media_row.id))
        response = await self.client.get(f'/api/chat/media/{media_row.id}')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'GIF89a-test')
        for path in ('config.json', '../outside.gif'):
            with self.store._db:
                self.store._db.execute('UPDATE outbox SET media_path=? WHERE id=?', (path, media_row.id))
            self.assertEqual((await self.client.get(f'/api/chat/media/{media_row.id}')).status, 404)
        with self.store._db:
            self.store._db.execute('UPDATE outbox SET media_path=? WHERE id=?', (str(self.store._workspace / 'config.json'), media_row.id))
        self.assertEqual((await self.client.get(f'/api/chat/media/{media_row.id}')).status, 404)
        self.client.session.headers.clear()
        self.assertEqual((await self.client.get(f'/api/chat/media/{media_row.id}')).status, 401)

    async def test_dashboard_replies_use_shared_owner_context(self):
        from dataclasses import replace
        import json
        from momoi.channel.napcat import NapCatConfig
        from momoi.integrations.models import LLMConfig
        from momoi.models import ProviderResponse, ToolCall
        from momoi.runtime.daemon import MomoiDaemon
        from tests.support import provider_catalog, reply_call, install_scripted_replyer, with_owner_recall
        config = ConfigurationManager(self.store._workspace / 'config.json').validate()
        config = replace(config, providers=provider_catalog(LLMConfig('http://127.0.0.1', 'test', 'test', 100, 0, 1, 0)),
                         channel=NapCatConfig('ws://127.0.0.1', '20000', 1, 60, 30, 30, 20))
        daemon = MomoiDaemon(config)
        self.addCleanup(daemon.store.close)
        install_scripted_replyer(daemon)
        case = self
        class Provider:
            calls = 0
            async def complete(self, _, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    case.assertIn('当前消息渠道：napcat（QQ 私聊）', json.dumps(messages, ensure_ascii=False))
                if self.calls == 3:
                    case.assertIn('当前消息渠道：dashboard（网页聊天）', json.dumps(messages, ensure_ascii=False))
                    case.assertIn('昨日在 QQ 聊的游戏', json.dumps(messages, ensure_ascii=False))
                    case.assertIn('dashboard', next(tool for tool in tools if tool['name'] == 'reply')['input_schema']['properties']['channel']['enum'])
                if self.calls % 2:
                    return ProviderResponse([], [reply_call(f'reply-{self.calls}', bubbles=['老师，我记得！'])])
                return ProviderResponse([], [ToolCall(f'end-{self.calls}', 'end_turn', {'mood': {'decision': 'unchanged'}})])
        daemon.provider = with_owner_recall(Provider())
        now = time.time()
        for index, (channel, text) in enumerate((('napcat', '昨日在 QQ 聊的游戏'), ('dashboard', '接着聊吧'))):
            stamp = time.time()
            event = IncomingMessage(f'{channel}:context', f'context{index}', text, stamp, stamp, channel=channel)
            daemon.store.add_event(event)
            turn = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn, 'owner', [event.event_id])
            await daemon._complete_batch([event], turn)
            for row in daemon.store.due_outbox():
                daemon.store.mark_sent(row.id)
        self.client.session.headers['Authorization'] = self.auth
        history = await (await self.client.get('/api/chat/messages')).json()
        self.assertEqual([row['content'] for row in history['messages']], ['接着聊吧', '老师，我记得！'])

    async def test_model_only_daemon_receives_and_delivers_web_chat(self):
        from dataclasses import replace
        from momoi.integrations.models import LLMConfig
        from momoi.models import ProviderResponse, ToolCall
        from momoi.runtime.daemon import MomoiDaemon
        from tests.support import provider_catalog, reply_call, install_scripted_replyer, with_owner_recall
        config = ConfigurationManager(self.store._workspace / 'config.json').validate()
        config = replace(config, providers=provider_catalog(LLMConfig('http://127.0.0.1', 'test', 'test', 100, 0, 1, 0)))
        self.assertEqual(config.channel_configs, ())
        daemon = MomoiDaemon(config)
        self.addCleanup(daemon.store.close)
        self.assertEqual(daemon.channel.name, 'dashboard')
        self.assertEqual(set(daemon.channels), {'dashboard'})
        install_scripted_replyer(daemon)
        class Provider:
            calls = 0
            async def complete(self, _, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ProviderResponse([], [reply_call('web-reply', bubbles=['老师，网页上也可以聊天！'])])
                return ProviderResponse([], [ToolCall('web-end', 'end_turn', {'mood': {'decision': 'unchanged'}})])
        daemon.provider = with_owner_recall(Provider())
        self.runtime.daemon = daemon
        self.client.session.headers['Authorization'] = self.auth
        response = await self.client.post('/api/chat/messages', json={'text': '只配模型可以聊天吗？', 'id': 'web_only_test'})
        self.assertEqual(response.status, 202)
        event = daemon.incoming.get_nowait()
        turn = daemon._turn_id(event.event_id)
        daemon.store.begin_turn(turn, 'owner', [event.event_id])
        await daemon._complete_batch([event], turn)
        stop = asyncio.Event()
        worker = asyncio.create_task(daemon._outbox_worker(stop))
        try:
            async with asyncio.timeout(2):
                while daemon.store.due_outbox():
                    await asyncio.sleep(0.01)
            data = await (await self.client.get('/api/chat/messages')).json()
            self.assertEqual(data['messages'][-1]['content'], '老师，网页上也可以聊天！')
            self.assertEqual(data['messages'][-1]['delivery_state'], 'delivered')
        finally:
            stop.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
