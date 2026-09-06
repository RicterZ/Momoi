import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


async def retry_call(
    operation: Callable[[], Awaitable[T]],
    *,
    delays: Sequence[float],
    retryable: Callable[[Exception], bool],
    on_error: Callable[[Exception, int, int, bool], None],
) -> T:
    """Retry only the errors selected by the capability; never swallow cancellation."""
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as error:
            again = attempt < attempts and retryable(error)
            on_error(error, attempt, attempts, again)
            if not again:
                raise
            await asyncio.sleep(delays[attempt - 1])
    raise AssertionError("unreachable")
