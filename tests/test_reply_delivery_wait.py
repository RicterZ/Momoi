import asyncio
from types import SimpleNamespace

from momoi.models import IncomingMessage
from momoi.runtime.agent.delivery import BubbleDelivery, DeliveryPolicy
from momoi.storage import Store


def test_reply_waits_for_partial_delivery_and_cancellation(tmp_path):
    store = Store(tmp_path / 'db')
    store.begin_turn('turn', 'owner', [])
    store.queue_progress('turn', 'reply', ['第一句', '第二句'], 'napcat')
    delivery = BubbleDelivery(store, {}, DeliveryPolicy(SimpleNamespace(), store), asyncio.Event())

    async def run():
        task = asyncio.create_task(delivery.wait_reply('turn', 'reply'))
        await asyncio.sleep(0)
        assert not task.done()
        first = store.due_outbox()[0]
        assert store.mark_sending(first.id)
        store.mark_sent(first.id)
        await asyncio.sleep(0)
        assert not task.done()
        store.cancel_pending_outbox('napcat', 'owner_message_superseded_outbox')
        result = await asyncio.wait_for(task, 1)
        assert result['state'] == 'interrupted'
        assert result['bubbles'] == ['第一句']
        assert result['cancelled'] == 1 and result['pending'] == 0
        assert '第二句' not in str(result)
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_reply_uncertain_and_timeout_never_claim_delivery(tmp_path):
    store = Store(tmp_path / 'db')
    store.begin_turn('turn', 'owner', [])
    store.queue_progress('turn', 'reply', ['不确定的一句'], 'napcat')
    delivery = BubbleDelivery(store, {}, DeliveryPolicy(SimpleNamespace(), store), asyncio.Event())
    try:
        result = asyncio.run(delivery.wait_reply('turn', 'reply', timeout=0))
        assert result['error'] == 'reply_delivery_timeout' and result['bubbles'] == []
        row = store.due_outbox()[0]
        store.mark_sending(row.id)
        store.mark_ambiguous(row.id, 2, 'lost_receipt')
        result = asyncio.run(delivery.wait_reply('turn', 'reply'))
        assert result['state'] == 'uncertain' and result['bubbles'] == []
        assert result['uncertain_bubbles'] == ['不确定的一句']
        store.mark_sent(row.id)
        result = asyncio.run(delivery.wait_reply('turn', 'reply'))
        assert result['state'] == 'sent' and result['bubbles'] == ['不确定的一句']
    finally:
        store.close()


def test_real_worker_partial_reply_steers_same_turn(tmp_path):
    import json
    from unittest.mock import patch
    from momoi.runtime import MomoiDaemon
    from momoi.models import ProviderResponse, ToolCall
    from tests.test_episode_annealing import config
    from tests.support import install_scripted_replyer, reply_call, with_owner_recall

    daemon = MomoiDaemon(config(str(tmp_path)))
    install_scripted_replyer(daemon)
    daemon.bubble_delivery.wait_reply = BubbleDelivery.wait_reply.__get__(daemon.bubble_delivery)
    sent = []
    rounds = []
    wire_requests = []
    stop = asyncio.Event()
    first = IncomingMessage('first', 'first', '说两句', 1, 1, channel=daemon.channel.name)
    update = IncomingMessage('update', 'update', '改主意了', 2, 2, channel=daemon.channel.name)

    async def send(payload):
        text = payload['segments'][0]['data']['text']
        sent.append(text)
        if text == '第一句':
            await daemon._receive(update)
        return str(len(sent))

    async def complete(system, messages, tools, **kwargs):
        from momoi.integrations.adapters.openai import openai_messages
        import copy
        wire = openai_messages(system, messages)
        if wire_requests:
            previous_wire, previous_tools = wire_requests[-1]
            assert wire[:len(previous_wire)] == previous_wire
            assert tools == previous_tools
        wire_requests.append((copy.deepcopy(wire), copy.deepcopy(tools)))
        rounds.append(json.dumps(messages, ensure_ascii=False))
        if len(rounds) == 1:
            call = reply_call('first-reply', bubbles=['第一句', '不要发的第二句'])
        elif len(rounds) == 2:
            assert sent == ['第一句']
            assert '[用户中途插话]' in rounds[-1] and '改主意了' in rounds[-1]
            results = [block for message in messages if isinstance(message.get('content'), list)
                       for block in message['content'] if block.get('type') == 'tool_result'
                       and block.get('tool_use_id') == 'first-reply']
            result = json.loads(results[0]['content'])
            assert result['state'] == 'interrupted' and result['bubbles'] == ['第一句']
            assert result['cancelled'] == 1
            call = reply_call('second-reply', bubbles=['收到改动'])
        else:
            assert sent == ['第一句', '收到改动']
            call = ToolCall('end', 'end_turn', {'mood': {'decision': 'unchanged'}})
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])

    async def run():
        daemon.provider = with_owner_recall(SimpleNamespace(complete=complete))
        daemon.store.add_event(first)
        worker = asyncio.create_task(daemon._outbox_worker(stop))
        try:
            with patch.object(daemon.channel, 'send_message', side_effect=send), patch('momoi.runtime.dispatch.delivery.random.uniform', return_value=0):
                await asyncio.wait_for(daemon._complete_batch_turn([first], stop, 'same-turn'), 3)
            assert sent == ['第一句', '收到改动']
            row = daemon.store._db.execute("SELECT source_ids_json FROM turns WHERE id='same-turn'").fetchone()
            assert json.loads(row[0]) == ['first', 'update']
        finally:
            stop.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()
