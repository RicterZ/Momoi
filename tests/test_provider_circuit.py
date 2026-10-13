import asyncio
from unittest.mock import patch

import pytest

from momoi.llm.errors import ProviderError
from momoi.llm.transport import ProviderCircuit, retry_request
from momoi.models import ProviderResponse


def test_permanent_http_failure_cools_all_following_calls_and_success_resets():
    async def run():
        circuit = ProviderCircuit()
        calls = 0
        async def fail(_):
            nonlocal calls
            calls += 1
            error = ProviderError('invalid model')
            error._http_status = 400
            raise error
        with patch('momoi.llm.transport.monotonic', return_value=100):
            with pytest.raises(ProviderError):
                await retry_request(protocol='openai', max_retries=6, request_fields={}, operation=fail, circuit=circuit)
            assert calls == 1
            assert circuit.retry_at == 400
        async def succeed(_):
            return ProviderResponse([], [])
        with patch('momoi.llm.transport.monotonic', return_value=101), patch('momoi.llm.transport.asyncio.sleep') as sleep:
            await retry_request(protocol='anthropic', max_retries=0, request_fields={}, operation=succeed, circuit=circuit)
            sleep.assert_awaited_once_with(299)
        assert circuit.retry_at == 0
    asyncio.run(run())


def test_circuit_wait_is_cancellable():
    async def run():
        circuit = ProviderCircuit()
        with patch('momoi.llm.transport.monotonic', return_value=0):
            circuit.retry_at = 300
            task = asyncio.create_task(circuit.wait())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
