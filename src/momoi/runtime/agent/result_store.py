import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


_RESULT_REF = re.compile(r"^tr_[0-9a-f]{32}$")
_CLEANUP_INTERVAL_SECONDS = 60 * 60


class ToolResultStore:
    """Private, file-backed snapshots for model-visible large tool results."""

    def __init__(self, root: Path, *, retention_days: float = 30) -> None:
        self.root = root.expanduser().resolve()
        self.retention_seconds = max(0.0, retention_days) * 24 * 60 * 60
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._last_cleanup = 0.0
        self.cleanup()

    def save(self, content: str) -> str:
        self._maybe_cleanup()
        result_ref = f"tr_{uuid.uuid4().hex}"
        path = self._path(result_ref)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.root,
                prefix=".pending-",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return result_ref

    def read(
        self,
        result_ref: object,
        cursor: object,
        *,
        max_chars: int,
        provenance: dict[str, str],
        status: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        reference = str(result_ref or "")
        try:
            path = self._path(reference)
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError, ValueError) as error:
            return {
                "ok": False,
                "error": "tool_result_unavailable",
                "message": str(error)[:500],
                "provenance": provenance,
            }
        try:
            offset = self._decode_cursor(reference, cursor)
        except ValueError as error:
            return {
                "ok": False,
                "error": "invalid_tool_result_cursor",
                "message": str(error)[:500],
                "provenance": provenance,
            }
        if offset > len(content):
            return {
                "ok": False,
                "error": "invalid_tool_result_cursor",
                "message": "Cursor is outside the stored tool result.",
                "provenance": provenance,
            }
        digest = hashlib.sha256(content.encode()).hexdigest()
        visible_status = status or {"ok": True, "error": None}

        def candidate(end: int) -> dict[str, Any]:
            has_more = end < len(content)
            return {
                **visible_status,
                "truncated": has_more,
                "provenance": provenance,
                "result_ref": reference,
                "format": "json",
                "sha256": digest,
                "original_chars": len(content),
                "chunk_start": offset,
                "chunk_end": end,
                "content": content[offset:end],
                "next_cursor": (
                    self._encode_cursor(reference, end) if has_more else None
                ),
                "has_more": has_more,
            }

        low, high = offset, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            rendered = json.dumps(
                candidate(middle), ensure_ascii=False, default=str
            )
            if len(rendered) <= max_chars:
                low = middle
            else:
                high = middle - 1
        result = candidate(low)
        if low == offset and offset < len(content):
            return {
                "ok": False,
                "error": "tool_result_chunk_limit_too_small",
                "message": "Tool result metadata exceeds the configured result limit.",
                "provenance": provenance,
            }
        return result

    def refit(self, value: str, *, max_chars: int) -> str | None:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        # A result returned inline carries its reference but no chunk bounds,
        # because nothing was omitted. It can still be shrunk the same way: the
        # snapshot is complete, so reading it back from the start yields a
        # smaller chunk plus a cursor instead of a truncated, unrecoverable body.
        start = parsed.get("chunk_start", 0) if isinstance(parsed, dict) else None
        if (
            not isinstance(parsed, dict)
            or not _RESULT_REF.fullmatch(str(parsed.get("result_ref") or ""))
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(parsed.get("provenance"), dict)
        ):
            return None
        result_ref = str(parsed["result_ref"])
        offset = int(start)
        cursor = None if offset == 0 else self._encode_cursor(result_ref, offset)
        result = self.read(
            result_ref,
            cursor,
            max_chars=max_chars,
            provenance={
                str(key): str(item)
                for key, item in parsed["provenance"].items()
            },
            status={
                key: parsed[key]
                for key in ("ok", "error", "message")
                if key in parsed
            },
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    def cleanup(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        self._last_cleanup = current
        if self.retention_seconds <= 0:
            return 0
        cutoff = current - self.retention_seconds
        removed = 0
        for path in self.root.glob("tr_*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    def _maybe_cleanup(self) -> None:
        if time.time() - self._last_cleanup >= _CLEANUP_INTERVAL_SECONDS:
            self.cleanup()

    def _path(self, result_ref: str) -> Path:
        if not _RESULT_REF.fullmatch(result_ref):
            raise ValueError("Invalid tool result reference.")
        return self.root / f"{result_ref}.json"

    @staticmethod
    def _encode_cursor(result_ref: str, offset: int) -> str:
        value = json.dumps(
            {"v": 1, "ref": result_ref, "offset": offset},
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(result_ref: str, cursor: object) -> int:
        if cursor in (None, ""):
            return 0
        encoded = str(cursor)
        encoded += "=" * (-len(encoded) % 4)
        try:
            value = json.loads(base64.urlsafe_b64decode(encoded).decode())
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid tool result cursor.") from error
        if (
            not isinstance(value, dict)
            or value.get("v") != 1
            or value.get("ref") != result_ref
            or not isinstance(value.get("offset"), int)
            or isinstance(value.get("offset"), bool)
            or value["offset"] < 0
        ):
            raise ValueError("Invalid tool result cursor.")
        return int(value["offset"])
