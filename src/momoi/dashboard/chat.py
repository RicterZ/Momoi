"""Authenticated private chat through the existing owner-message runtime."""
import re
import time
from pathlib import Path
from aiohttp import web
from ..models import IncomingMessage


def register_chat_routes(app, store, runtime):
    async def history(request):
        try:
            before = float(request.query.get("before", "inf"))
            limit = min(100, max(1, int(request.query.get("limit", "100"))))
            if before != before or before <= 0:
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text="invalid pagination") from None
        before_id = request.query.get("before_id", "")
        rows = store._db.execute("""SELECT * FROM (SELECT id, content, occurred_at AS created_at,
                   'user' AS role, 'received' AS delivery_state, NULL AS media_id, 'text' AS kind FROM events
                   WHERE json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END, '$.channel')='dashboard'
                   UNION ALL SELECT 'reply:' || o.id, o.text,
                   COALESCE(m.created_at, p.created_at, t.started_at), 'assistant',
                   CASE WHEN o.state='sent' THEN 'delivered'
                        WHEN o.state IN ('pending','sending') THEN 'queued'
                        WHEN o.state='superseded' THEN 'cancelled'
                        WHEN o.state='failed' THEN 'failed' ELSE 'uncertain' END,
                   CASE WHEN o.media_path IS NOT NULL THEN o.id ELSE NULL END, o.kind
                   FROM outbox o JOIN turns t ON t.id=o.turn_id
                   LEFT JOIN messages m ON m.outbox_id=o.id
                   LEFT JOIN turn_progress p ON o.dedupe_key='turn:' || p.turn_id || ':progress:' || p.tool_call_id || ':' || p.part_index
                   WHERE o.target_channel='dashboard'
                   ) WHERE created_at < ? OR (created_at = ? AND id < ?)
                   ORDER BY created_at DESC, id DESC LIMIT ?""", (before, before, before_id, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        status = runtime.status()
        daemon = runtime.daemon
        channel = getattr(daemon, "channels", {}).get("dashboard")
        active = daemon is not None and getattr(daemon, "_active_turn_channel", "") == "dashboard" and getattr(daemon, "_active_turn", None) is not None
        pending = store._db.execute("SELECT 1 FROM events WHERE processed=0 AND json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END, '$.channel')='dashboard' LIMIT 1").fetchone() is not None
        return web.json_response({"messages": [dict(row) for row in reversed(rows)],
                                  "has_more": has_more, "runtime": status,
                                  "typing": bool(getattr(channel, "typing", False) or active or pending) and status["runtime_active"]})

    async def send(request):
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="invalid json") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="invalid message")
        text, key = body.get("text"), body.get("id")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise web.HTTPBadRequest(text="消息需要为 1–10000 字符")
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", key):
            raise web.HTTPBadRequest(text="invalid message id")
        event_id = f"dashboard:{key}"
        async with runtime.lock:
            existing = store._db.execute("SELECT content FROM events WHERE id=?", (event_id,)).fetchone()
            if existing is not None:
                if existing[0] != text.strip():
                    raise web.HTTPConflict(text="消息 ID 已被使用")
                return web.json_response({"id": event_id}, status=200)
            status = runtime.status()
            if runtime.suspended or not status["runtime_active"] or status.get("budget", {}).get("blocked"):
                raise web.HTTPConflict(text="Momoi 目前暂停运行，请检查设置或费用预算")
            daemon = runtime.daemon
            if "dashboard" not in getattr(daemon, "channels", {}):
                raise web.HTTPServiceUnavailable(text="聊天频道尚未就绪")
            now = time.time()
            event = IncomingMessage(event_id=event_id, message_id=key, text=text.strip(),
                                    occurred_at=now, received_at=now, channel="dashboard")
            await daemon._receive(event)
        return web.json_response({"id": event_id}, status=202)

    async def media(request):
        try:
            identifier = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound() from None
        row = store._db.execute("SELECT media_path FROM outbox WHERE id=? AND target_channel='dashboard'", (identifier,)).fetchone()
        if row is None or not row[0]:
            raise web.HTTPNotFound()
        workspace = store._workspace
        path = Path(row[0])
        path = (path if path.is_absolute() else workspace / path).resolve()
        if not any(path.is_relative_to((workspace / name).resolve()) for name in ("artifacts", "emotion", "channel")) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})

    app.router.add_get("/api/chat/media/{id}", media)
    app.router.add_get("/api/chat/messages", history)
    app.router.add_post("/api/chat/messages", send)
