"""Bounded process output and process-group cleanup shared with Webhooks."""

import asyncio
import os
import signal
import subprocess
from pathlib import Path


async def terminate_process(process: asyncio.subprocess.Process, completion, job=None) -> None:
    if os.name == "nt":
        if job is not None:
            job.close()
        elif process.returncode is None:
            process.kill()
        await process.wait()
        return
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
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000004} if os.name == "nt" else {"start_new_session": True})
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **options,
    )

    job = None
    if os.name == "nt":
        from ..platform.windows import ProcessJob, resume_process

        try:
            job = ProcessJob(process.pid)
            resume_process(process.pid)
        except OSError:
            if job is not None:
                job.close()
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise

    async def capture(stream):
        marker = b"\n[...truncated...]\n"
        limit = max(1, output_limit)
        if limit <= len(marker):
            head_limit = 0
            tail_limit = limit
        else:
            head_limit = (limit - len(marker)) // 2
            tail_limit = limit - len(marker) - head_limit
        output = bytearray()
        head: bytes | None = None
        tail = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            if head is None:
                output.extend(chunk)
                if len(output) <= limit:
                    continue
                head = bytes(output[:head_limit])
                tail = output[-tail_limit:]
                output.clear()
                truncated = True
            else:
                tail.extend(chunk)
                del tail[:-tail_limit]
        if not truncated:
            return output.decode(errors="replace"), False
        if head_limit == 0:
            return tail.decode(errors="replace"), True
        assert head is not None
        # Keep complete lines at the cut when possible. Very long lines retain
        # their beginning and end as fragments.
        head_break = head.rfind(b"\n")
        if head_break >= 0:
            head = head[: head_break + 1]
        tail_break = tail.find(b"\n")
        if tail_break >= 0:
            tail = tail[tail_break + 1:]
        separator = marker.lstrip(b"\n") if head.endswith(b"\n") else marker
        return (head + separator + tail).decode(errors="replace"), True

    readers = asyncio.gather(capture(process.stdout), capture(process.stderr), process.wait())
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(asyncio.shield(readers), timeout)
    except BaseException:
        try:
            await terminate_process(process, readers, job)
        finally:
            readers.cancel()
            await asyncio.gather(readers, return_exceptions=True)
        raise
    finally:
        if job is not None:
            job.close()
    return {
        "exit_code": exit_code, "stdout_tail": stdout[0], "stderr_tail": stderr[0],
        "truncated": stdout[1] or stderr[1],
    }
