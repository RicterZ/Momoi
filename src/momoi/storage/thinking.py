import gzip
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_context import log_event
from ..search import (
    SearchBackend,
    StringSearchBackend,
    alternative_weights,
    document_frequency,
    search_alternatives,
    search_expression,
)

logger = logging.getLogger(__name__)

_FILE = re.compile(r"^thinking-(\d{4}-\d{2})\.sqlite3$")
_GZIP_MIN_BYTES = 1024
_MAX_ALL_MONTHS = 36
_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  created_at REAL NOT NULL,
  turn_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  round INTEGER NOT NULL,
  model TEXT NOT NULL,
  tools_json TEXT NOT NULL,
  reasoning_chars INTEGER NOT NULL,
  reasoning_sha256 TEXT NOT NULL,
  reasoning_codec TEXT NOT NULL,
  reasoning_blob BLOB
);
CREATE INDEX IF NOT EXISTS calls_turn ON calls(turn_id, round);
CREATE INDEX IF NOT EXISTS calls_time ON calls(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS calls_call_id ON calls(call_id);
"""


def month_key(when: float) -> str:
    return datetime.fromtimestamp(when).astimezone().strftime("%Y-%m")


def parse_month(value: str) -> str:
    text = str(value or "").strip()
    if not _FILE.fullmatch(f"thinking-{text}.sqlite3"):
        raise ValueError("invalid_month")
    year, month_number = (int(part) for part in text.split("-"))
    if not 1 <= month_number <= 12:
        raise ValueError("invalid_month")
    return text


def month_bounds(month: str) -> tuple[float, float]:
    year, month_number = (int(part) for part in parse_month(month).split("-"))
    start = datetime(year, month_number, 1).astimezone()
    if month_number == 12:
        end = datetime(year + 1, 1, 1).astimezone()
    else:
        end = datetime(year, month_number + 1, 1).astimezone()
    return start.timestamp(), end.timestamp()


def encode_reasoning(text: str) -> tuple[str, bytes]:
    raw = text.encode("utf-8")
    if len(raw) < _GZIP_MIN_BYTES:
        return "plain", raw
    return "gzip", gzip.compress(raw, compresslevel=6)


def decode_reasoning(codec: str, blob: bytes | None) -> str:
    data = blob or b""
    if codec == "gzip":
        return gzip.decompress(data).decode("utf-8")
    return data.decode("utf-8")


class ThinkingStore:
    def __init__(
        self,
        directory: Path,
        search_backend: SearchBackend | None = None,
    ) -> None:
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._search_backend = search_backend or StringSearchBackend()
        self._dbs: dict[str, sqlite3.Connection] = {}

    def close(self) -> None:
        for connection in self._dbs.values():
            connection.close()
        self._dbs.clear()

    def available_months(self) -> list[str]:
        return self._available_months()

    def record(
        self,
        *,
        created_at: float,
        turn_id: str,
        call_id: str,
        stage: str,
        round: int,
        model: str,
        tools: list[str],
        reasoning: str,
    ) -> None:
        text = str(reasoning or "")
        codec, blob = encode_reasoning(text)
        month = month_key(created_at)
        connection = self._db(month)
        with connection:
            connection.execute(
                """INSERT OR REPLACE INTO calls
                   (created_at, turn_id, call_id, stage, round, model, tools_json,
                    reasoning_chars, reasoning_sha256, reasoning_codec, reasoning_blob)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    created_at,
                    str(turn_id or ""),
                    str(call_id or uuid.uuid4().hex),
                    str(stage or ""),
                    max(0, int(round)),
                    str(model or ""),
                    json.dumps(list(tools), ensure_ascii=False, separators=(",", ":")),
                    len(text),
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    codec,
                    blob,
                ),
            )

    def search(
        self,
        *,
        turn_id: str = "",
        query: str = "",
        after: float | None = None,
        before: float | None = None,
        stage: str = "",
        limit: int = 5,
        cursor: int = 0,
        hint_at: float | None = None,
    ) -> dict[str, Any]:
        months = self._months_for(after, before, hint_at=hint_at, turn_id=turn_id)
        rows: list[dict[str, Any]] = []
        for month in months:
            rows.extend(self._scan_month(month, turn_id, after, before, stage))
        rows.sort(key=lambda item: (-float(item["created_at"]), int(item["round"])))
        reasonings = [
            decode_reasoning(row["reasoning_codec"], row["reasoning_blob"])
            for row in rows
        ]
        weights = (
            alternative_weights(
                document_frequency(
                    search_alternatives(query),
                    ((reasoning,) for reasoning in reasonings),
                    self._search_backend,
                ),
                len(reasonings),
            )
            if query.strip()
            else None
        )
        matched: list[dict[str, Any]] = []
        for row, reasoning in zip(rows, reasonings):
            excerpt = reasoning
            if query.strip():
                found = search_expression(
                    query, (reasoning,), self._search_backend, weights=weights
                )
                if found is None:
                    continue
                excerpt = _keyword_excerpt(reasoning, found.alternatives)
            elif not turn_id:
                excerpt = reasoning[:400]
            matched.append(_public_call(row, excerpt=excerpt))
        page = matched[cursor : cursor + limit]
        result: dict[str, Any] = {
            "ok": True,
            "count": len(page),
            "calls": page,
        }
        next_cursor = cursor + limit
        if next_cursor < len(matched):
            result["next_cursor"] = next_cursor
        return result

    def read(self, turn_id: str, call_id: str = "") -> dict[str, Any]:
        if not turn_id.strip() and not call_id.strip():
            return {"ok": False, "error": "missing_turn_id"}
        months = (
            self._months_for(None, None, turn_id=turn_id)
            if turn_id.strip()
            else self._available_months()[-_MAX_ALL_MONTHS :]
        )
        found: list[dict[str, Any]] = []
        for month in months:
            found.extend(
                self._scan_month(month, turn_id, None, None, "", call_id=call_id)
            )
        found.sort(key=lambda item: (float(item["created_at"]), int(item["round"])))
        if not found:
            return {"ok": False, "error": "thinking_not_found"}
        return {
            "ok": True,
            "count": len(found),
            "calls": [
                _public_call(
                    row,
                    reasoning=decode_reasoning(
                        row["reasoning_codec"], row["reasoning_blob"]
                    ),
                )
                for row in found
            ],
        }

    def _months_for(
        self,
        after: float | None,
        before: float | None,
        *,
        hint_at: float | None = None,
        turn_id: str = "",
    ) -> list[str]:
        available = self._available_months()
        if hint_at is not None:
            key = month_key(hint_at)
            nearby = [month for month in available if abs(_month_index(month) - _month_index(key)) <= 1]
            if nearby:
                return nearby
        selected = [
            month
            for month in available
            if _month_overlaps(month, after, before)
        ]
        if turn_id and after is None and before is None and hint_at is None:
            selected = available[-_MAX_ALL_MONTHS :]
        elif len(selected) > _MAX_ALL_MONTHS:
            selected = selected[-_MAX_ALL_MONTHS :]
        return selected

    def _available_months(self) -> list[str]:
        months: list[str] = []
        for path in self.directory.glob("thinking-*.sqlite3"):
            match = _FILE.fullmatch(path.name)
            if match:
                months.append(match.group(1))
        return sorted(set(months))

    def _scan_month(
        self,
        month: str,
        turn_id: str,
        after: float | None,
        before: float | None,
        stage: str,
        call_id: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        values: list[object] = []
        if turn_id:
            clauses.append("turn_id=?")
            values.append(turn_id)
        if call_id:
            clauses.append("call_id=?")
            values.append(call_id)
        if after is not None:
            clauses.append("created_at>=?")
            values.append(after)
        if before is not None:
            clauses.append("created_at<?")
            values.append(before)
        if stage:
            clauses.append("stage=?")
            values.append(stage)
        rows = self._db(month).execute(
            f"""SELECT created_at, turn_id, call_id, stage, round, model, tools_json,
                       reasoning_chars, reasoning_codec, reasoning_blob
                FROM calls WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, round""",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def _db(self, month: str) -> sqlite3.Connection:
        existing = self._dbs.get(month)
        if existing is not None:
            return existing
        connection = sqlite3.connect(self.directory / f"thinking-{month}.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        self._dbs[month] = connection
        return connection


def _month_index(month: str) -> int:
    year, month_number = (int(part) for part in month.split("-"))
    return year * 12 + month_number


def _month_overlaps(
    month: str, after: float | None, before: float | None
) -> bool:
    start, end = month_bounds(month)
    if after is not None and end <= after:
        return False
    if before is not None and start >= before:
        return False
    return True


def _keyword_excerpt(text: str, alternatives: tuple[str, ...]) -> str:
    folded = text.casefold()
    for term in alternatives:
        index = folded.find(term.casefold())
        if index < 0:
            continue
        start = max(0, index - 80)
        end = min(len(text), index + len(term) + 160)
        prefix = "…" if start else ""
        suffix = "…" if end < len(text) else ""
        return prefix + text[start:end] + suffix
    return text[:400]


def _public_call(
    row: dict[str, Any],
    *,
    excerpt: str | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    tools = json.loads(str(row["tools_json"] or "[]"))
    item = {
        "turn_id": row["turn_id"],
        "call_id": row["call_id"],
        "created_at": row["created_at"],
        "stage": row["stage"],
        "round": row["round"],
        "model": row["model"],
        "tools": tools if isinstance(tools, list) else [],
        "reasoning_chars": row["reasoning_chars"],
    }
    if excerpt is not None:
        item["excerpt"] = excerpt
    if reasoning is not None:
        item["reasoning"] = reasoning
    return item


def persist_thinking_failure(error: Exception) -> None:
    log_event(
        logger,
        logging.WARNING,
        "thinking_record_failed",
        error_type=type(error).__name__,
        exc_info=True,
    )
