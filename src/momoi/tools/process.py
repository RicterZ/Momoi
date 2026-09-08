"""Bounded process output and process-group cleanup shared with Webhooks."""

import asyncio
import os
import signal
from pathlib import Path


async def terminate_process(process: asyncio.subprocess.Process, completion) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(asyncio.shield(completion), timeout=1)
        return
    except TimeoutError:
        pass
    # The leader may have exited while descendants still hold output pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def run_process(
    argv: list[str], *, timeout: float, cwd: Path | None = None,
    env: dict[str, str] | None = None, output_limit: int = 16384,
) -> dict:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )

    async def tail(stream):
        output = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            output.extend(chunk)
            if len(output) > output_limit:
                del output[:-output_limit]
                truncated = True
        return output.decode(errors="replace"), truncated

    readers = asyncio.gather(tail(process.stdout), tail(process.stderr), process.wait())
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(asyncio.shield(readers), timeout)
    except BaseException:
        try:
            await terminate_process(process, readers)
        finally:
            readers.cancel()
            await asyncio.gather(readers, return_exceptions=True)
        raise
    return {
        "exit_code": exit_code, "stdout_tail": stdout[0], "stderr_tail": stderr[0],
        "truncated": stdout[1] or stderr[1],
    }
