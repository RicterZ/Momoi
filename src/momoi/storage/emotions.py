import sqlite3
import time
from pathlib import Path

from ..emotions import valid_emotion_slug
from .memory import estimate_tokens


class EmotionStore:
    """Emotion asset catalog and storage-path policy."""

    def add_emotion(
        self, slug: str, path: str | Path, description: str
    ) -> dict[str, object]:
        slug = slug.strip()
        description = description.strip()
        asset = self._resolve_asset_path(path)
        if not valid_emotion_slug(slug):
            raise ValueError(
                "slug must use lowercase letters, digits, dot, underscore, or hyphen"
            )
        if not asset.is_file():
            raise ValueError("path must be an existing file")
        if not description or len(description) > 500:
            raise ValueError("description must contain 1 to 500 characters")
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO emotions(slug, path, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET path=excluded.path,
                     description=excluded.description, updated_at=excluded.updated_at""",
                (slug, self._stored_asset_path(asset), description, now, now),
            )
        return self.emotion(slug) or {}

    def delete_emotion(self, slug: str) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM emotions WHERE slug=?", (slug,))
        return cursor.rowcount == 1

    def emotion(self, slug: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT id, slug, path, description FROM emotions WHERE slug=?", (slug,)
        ).fetchone()
        return self._emotion_dict(row) if row else None

    def list_emotions(self) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT id, slug, path, description FROM emotions ORDER BY id"
        ).fetchall()
        return [self._emotion_dict(row) for row in rows]

    def _emotion_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["path"] = str(self._resolve_asset_path(str(item["path"])))
        return item

    def _resolve_asset_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self._workspace / path).resolve()

    def _stored_asset_path(self, value: str | Path) -> str:
        path = self._resolve_asset_path(value)
        try:
            return path.relative_to(self._workspace).as_posix()
        except ValueError:
            return str(path)

    def _recover_emotion_outbox(self) -> None:
        with self._db:
            failed = self._db.execute(
                """SELECT id, media_path FROM outbox
                   WHERE state='failed' AND kind='image'
                     AND last_error LIKE 'media asset cannot be read:%'"""
            ).fetchall()
            for message in failed:
                if (
                    message["media_path"]
                    and self._resolve_asset_path(str(message["media_path"])).is_file()
                ):
                    self._db.execute(
                        """UPDATE outbox SET state='pending', attempts=0,
                           last_error=NULL, next_attempt_at=0 WHERE id=?""",
                        (message["id"],),
                    )
                    self._sync_outbox_message(int(message["id"]), "pending")

    def emotion_path_referenced(
        self, path: str, *, exclude_slug: str | None = None
    ) -> bool:
        path = self._stored_asset_path(path)
        return (
            self._db.execute(
                """SELECT 1 FROM emotions
               WHERE path=? AND (? IS NULL OR slug<>?)
               UNION ALL
               SELECT 1 FROM outbox
               WHERE media_path=? AND state NOT IN ('sent', 'failed', 'superseded')
               UNION ALL
               SELECT 1 FROM notifications AS n
               JOIN emotions AS e ON e.path=?
               WHERE n.state='pending'
                 AND instr(n.messages_json, 'emotion://' || e.slug) > 0
               LIMIT 1""",
                (path, exclude_slug, exclude_slug, path, path),
            ).fetchone()
            is not None
        )

    def emotion_context(self, token_budget: int = 4000) -> str:
        lines: list[str] = []
        tokens = 0
        for row in self.list_emotions():
            line = f"- slug={row['slug']} meaning={row['description']}"
            line_tokens = estimate_tokens(line)
            if tokens + line_tokens > token_budget:
                break
            lines.append(line)
            tokens += line_tokens
        return "\n".join(lines)
