import asyncio
import json
from contextlib import aclosing

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.adapters.openai_responses import OpenAIResponsesProvider
from momoi.integrations.adapters.anthropic import AnthropicProvider
from momoi.integrations.models import LLMConfig
from momoi.llm.errors import ProviderResponseError
from momoi.llm.streaming import StreamingProvider


def wire_events(protocol):
    if protocol == 'openai':
        return [
            {'choices': [{'delta': {'reasoning_content': 'think'}}]},
            {'choices': [{'delta': {'content': '你好'}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call1', 'function': {'name': 'read', 'arguments': '{"x":'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '1}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3}},
            '[DONE]',
        ]
    if protocol == 'openai_responses':
        return [
            {'type': 'response.reasoning_text.delta', 'delta': 'think'},
            {'type': 'response.output_text.delta', 'delta': '你好'},
            {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '{"x":1}'},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
                {'type': 'reasoning', 'id': 'rs1', 'encrypted_content': 'opaque', 'content': [{'type': 'reasoning_text', 'text': 'think'}]},
                {'type': 'message', 'content': [{'type': 'output_text', 'text': '你好'}]},
                {'type': 'function_call', 'id': 'fc1', 'call_id': 'call1', 'name': 'read', 'arguments': '{"x":1}'},
            ], 'usage': {'input_tokens': 10, 'output_tokens': 3}}},
        ]
    return [
        {'type': 'message_start', 'message': {'content': [], 'usage': {'input_tokens': 10}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'thinking': 'think'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'signature': 'opaque'}},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'text': '你好'}},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'call1', 'name': 'read', 'input': {}}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'partial_json': '{"x":'}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'partial_json': '1}'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 3}},
        {'type': 'message_stop'},
    ]


@pytest.mark.parametrize('protocol,provider_class,path', [
    ('openai', OpenAIProvider, '/v1/chat/completions'),
    ('openai_responses', OpenAIResponsesProvider, '/v1/responses'),
    ('anthropic', AnthropicProvider, '/v1/messages'),
])
@pytest.mark.parametrize('truncated', [False, True])
def test_stream_protocols(protocol, provider_class, path, truncated):
    async def scenario():
        requests = []
        async def handler(request):
            payload = await request.json()
            requests.append(payload)
            assert payload['stream'] is True
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            events = wire_events(protocol)
            if truncated:
                events = events[:-1]
            await response.write(b': keepalive\r\n\r\n')
            for event in events:
                raw = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
                data = ('data: ' + raw + '\r\n\r\n').encode()
                # Split UTF-8 and field boundaries across transport chunks.
                for i in range(0, len(data), 7):
                    await response.write(data[i:i+7])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post(path, handler)
        async with TestServer(app) as server:
            config = LLMConfig(str(server.make_url('/')).rstrip('/'), 'secret', 'test', 100, 0, 5, 2)
            async with provider_class(config) as provider:
                events = []
                async def consume():
                    async with aclosing(provider.stream('system', [{'role': 'user', 'content': 'hi'}])) as stream:
                        async for event in stream:
                            events.append(event)
                if truncated:
                    with pytest.raises(ProviderResponseError, match='before completion'):
                        await consume()
                    assert not any(e.type == 'response' for e in events)
                else:
                    await consume()
                    assert ''.join(e.delta for e in events if e.type == 'text_delta') == '你好'
                    assert ''.join(e.delta for e in events if e.type == 'reasoning_delta') == 'think'
                    result = events[-1].data
                    assert result.tool_calls[0].arguments == {'x': 1}
                    assert result.usage['input'] == 10
                    assert result.usage['output'] == 3
                    if protocol == 'openai_responses':
                        assert result.continuation['openai_responses']['output'][0]['encrypted_content'] == 'opaque'
                    if protocol == 'anthropic':
                        assert result.content[0]['signature'] == 'opaque'
        assert len(requests) == 1
    asyncio.run(scenario())


def test_close_cancels_upstream():
    from momoi.llm.streaming import ProviderEvent, _stream
    async def scenario():
        cancelled = asyncio.Event()
        class Provider(StreamingProvider):
            async def complete(self):
                try:
                    await _stream.get()(ProviderEvent('text_delta', 'first'))
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        async with aclosing(Provider().stream()) as stream:
            assert (await anext(stream)).delta == 'first'
        assert cancelled.is_set()
    asyncio.run(scenario())


def test_image_router_streams_only_primary_output():
    from types import SimpleNamespace
    from momoi.models import ProviderResponse
    from momoi.integrations.image_routing import ImageRoutingModel
    from momoi.llm.streaming import ProviderEvent, _stream

    async def scenario():
        async def vision_complete(*args):
            assert _stream.get() is None
            return ProviderResponse([{'type': 'text', 'text': 'recognized image'}], [])

        async def primary_complete(system, messages, *args, **kwargs):
            assert 'recognized image' in str(messages)
            await _stream.get()(ProviderEvent('text_delta', 'visible'))
            return ProviderResponse([{'type': 'text', 'text': 'visible'}], [])

        fields = dict(usage_sink=None, thinking_sink=None, usage_parser=None)
        model = ImageRoutingModel(SimpleNamespace(**fields, complete=primary_complete),
                                  SimpleNamespace(**fields, complete=vision_complete))
        messages = [{'role': 'user', 'content': [{'type': 'image', 'source': {'data': 'test'}}]}]
        events = [event async for event in model.stream('system', messages)]
        assert [event.type for event in events] == ['text_delta', 'response']
        assert events[0].delta == 'visible'
        assert messages[0]['content'][0]['type'] == 'image'
    asyncio.run(scenario())
