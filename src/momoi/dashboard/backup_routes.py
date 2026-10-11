import asyncio
import tempfile
import sqlite3
from contextlib import closing
import zipfile
from pathlib import Path

from aiohttp import web
from .backup import export_archive, validate_archive, restore_archive, MAX_BYTES


async def finish_thread(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the workspace paused and temporary files alive until the writer exits.
        await task
        raise


def register_backup_routes(app, store, configuration, runtime):
    lock = asyncio.Lock()

    async def export(request):
        if lock.locked() or runtime.suspended:
            raise web.HTTPConflict(text="运行正在暂停或处理其他备份操作")
        async with lock:
            with tempfile.TemporaryDirectory(prefix="momoi-backup-") as directory:
                try:
                    await runtime.pause()
                    config = configuration.validate()
                    archive = await finish_thread(export_archive, config, directory)
                finally:
                    runtime.resume()
                response = web.StreamResponse(headers={"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="momoi-backup.zip"'})
                await response.prepare(request)
                with archive.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        await response.write(chunk)
                await response.write_eof()
                return response

    async def restore(request):
        if lock.locked() or runtime.suspended:
            raise web.HTTPConflict(text="运行正在暂停或处理其他备份操作")
        async with lock:
            with tempfile.TemporaryDirectory(prefix="momoi-restore-") as directory:
                root = Path(directory)
                upload = root / "upload.zip"
                size = 0
                with upload.open("wb") as output:
                    async for chunk in request.content.iter_chunked(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BYTES, actual_size=size)
                        output.write(chunk)
                # Validate before replacing anything or interrupting the conversation.
                try:
                    config = configuration.validate()
                    def validate():
                        with closing(sqlite3.connect(config.database)) as db:
                            return validate_archive(upload, root / "extracted", db)
                    extracted = await finish_thread(validate)
                except (ValueError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile, OSError) as error:
                    raise web.HTTPBadRequest(text=f"无法恢复备份：{error}") from None
                try:
                    await runtime.pause()
                    restore_archive(extracted, configuration.validate(), store)
                finally:
                    runtime.resume()
                return web.json_response({"ok": True, "message": "已恢复身份、聊天和长期状态，正在重新连接。"})

    app.router.add_post("/api/settings/backup/export", export)
    app.router.add_post("/api/settings/backup/restore", restore)
