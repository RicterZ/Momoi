import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ...models import IncomingMessage, OwnerInputStatus
from .. import NotConnected, SendRejected
from .config import WechatyConfig

logger = logging.getLogger(__name__)


class WechatyChannel:
    name = "wechaty"

    def __init__(self, config: WechatyConfig):
        self.config = config
        self.quiet_seconds = config.quiet_seconds
        self.max_batch_seconds = config.max_batch_seconds
        self.login_state: dict[str, str] = {"status": "idle"}
        self._process = None
        self._run_task: asyncio.Task | None = None
        self._writer_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._login_requested = asyncio.Event()

    async def start_login(self):
        if self._process is None and (self._run_task is None or self._run_task.done()):
            raise RuntimeError("Wechaty channel is not running")
        self._login_requested.set()
        self.login_state = {"status": "starting"}
        return self.login_state

    async def cancel_login(self):
        self._login_requested.clear()
        if self._process and self._process.returncode is None:
            await self._command({"command": "cancel"})
        self.login_state = {"status": "cancelled"}

    async def run(self, on_event: Callable, stop: asyncio.Event) -> None:
        self._run_task = asyncio.current_task()
        node = os.environ.get("MOMOI_NODE") or shutil.which("node")
        worker_dir = Path(os.environ.get("MOMOI_WECHATY_WORKER_DIR") or Path(__file__).parent / "worker")
        script = worker_dir / "worker.js"
        if not node or not script.is_file():
            self.login_state = {"status": "error", "error": "node_worker_unavailable"}
            await stop.wait()
            return
        self.config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {**os.environ, "HOME": str(self.config.data_dir), "WECHATY_PUPPET_WECHAT4U_TOKEN_DIR": str(self.config.data_dir),
               "MOMOI_WECHATY_OWNER_ALIAS": self.config.owner_alias}
        self._process = await asyncio.create_subprocess_exec(
            node, str(script), cwd=self.config.data_dir, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        reader = asyncio.create_task(self._read_events(on_event))
        errors = asyncio.create_task(self._read_stderr())
        if (self.config.data_dir / "logged-in").is_file():
            self._login_requested.set()
        try:
            while not stop.is_set() and self._process.returncode is None:
                if self._login_requested.is_set():
                    self._login_requested.clear()
                    await self._command({"command": "start"})
                try:
                    await asyncio.wait_for(stop.wait(), 0.25)
                except TimeoutError:
                    pass
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), 5)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
        finally:
            reader.cancel()
            errors.cancel()
            await asyncio.gather(reader, errors, return_exceptions=True)
            self._process = None
            self._run_task = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(NotConnected("Wechaty worker stopped"))
            self._pending.clear()

    async def _read_stderr(self):
        assert self._process and self._process.stderr
        while line := await self._process.stderr.readline():
            logger.info("Wechaty worker: %s", line.decode(errors="replace").rstrip()[:500])

    async def _read_events(self, on_event):
        assert self._process and self._process.stdout
        while line := await self._process.stdout.readline():
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Ignoring invalid Wechaty worker event")
                continue
            kind = event.get("type")
            if kind == "scan":
                state = "waiting" if event.get("status") in {"Waiting", "Timeout"} else "scanned"
                self.login_state = {"status": state, "qr_content": str(event.get("qrcode") or "")}
            elif kind == "login":
                self.login_state = {"status": "confirmed", "user": str(event.get("user") or "")}
                (self.config.data_dir / "logged-in").touch(mode=0o600, exist_ok=True)
                await on_event(OwnerInputStatus(self.name))
            elif kind == "logout":
                self.login_state = {"status": "idle"}
            elif kind == "login_cancelled":
                self.login_state = {"status": "cancelled"}
            elif kind == "error":
                self.login_state = {"status": "error", "error": "wechaty_worker_error"}
                logger.warning("Wechaty worker reported an error")
            elif kind == "message":
                sender_id = str(event.get("senderId") or "")
                text = str(event.get("text") or "").strip()
                if not sender_id or not text:
                    continue
                message_id = str(event.get("id") or "")
                if not message_id:
                    continue
                await on_event(IncomingMessage(
                    event_id=f"wechaty:{sender_id}:{message_id}", message_id=message_id,
                    text=text, occurred_at=time.time(), received_at=time.time(),
                    segments=({"type": "text", "data": {"text": text}},),
                    channel=self.name, delivery_context={"wechaty_recipient": sender_id},
                ))
            elif kind == "response":
                future = self._pending.pop(str(event.get("id")), None)
                if future and not future.done():
                    if event.get("ok"):
                        future.set_result(True)
                    else:
                        future.set_exception(SendRejected("Wechaty rejected message"))

    async def _command(self, value: dict) -> None:
        if not self._process or self._process.stdin is None:
            raise NotConnected("Wechaty worker is not running")
        async with self._writer_lock:
            self._process.stdin.write((json.dumps(value) + "\n").encode())
            await self._process.stdin.drain()

    async def send_message(self, payload: dict) -> str:
        if payload.get("action") != "message":
            raise SendRejected("Wechaty supports text messages only")
        recipient = str((payload.get("delivery_context") or {}).get("wechaty_recipient") or "")
        text = "\n".join(str(segment.get("data", {}).get("text") or "")
                           for segment in payload.get("segments", [])
                           if segment.get("type") == "text").strip()
        if not (recipient or self.config.owner_alias) or not text:
            raise SendRejected("Wechaty requires owner_alias and text")
        request_id = str(time.monotonic_ns())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._command({"command": "send", "id": request_id, "recipient": recipient, "text": text})
            await asyncio.wait_for(future, 30)
        finally:
            self._pending.pop(request_id, None)
        return request_id

    def content_blocks(self, segments):
        return [{"type": "text", "text": item.get("data", {}).get("text", "")}
                for item in segments if item.get("type") == "text"]

    def workflow_variables(self):
        return {"channel": "wechaty"}

    async def convert_voice(self, voice):
        return None
