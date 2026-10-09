"""Persistent idempotency receipts in the caller's SQLite transaction."""
import hashlib
import json
import sqlite3

from ..writing.models import canonical_json


class MemoryCommits:
    def __init__(self, database: sqlite3.Connection) -> None:
        self.database = database

    @staticmethod
    def input_hash(payload: object) -> str:
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def result(self, operation_id: str, input_hash: str):
        row = self.database.execute(
            'SELECT input_hash, result_json FROM memory_commits WHERE operation_id=?',
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if row['input_hash'] != input_hash:
            raise ValueError('memory_operation_id_conflict')
        return json.loads(row['result_json'])

    def record(self, operation_id: str, input_hash: str, result, now: float) -> None:
        self.database.execute(
            'INSERT INTO memory_commits(operation_id,input_hash,result_json,committed_at) VALUES (?,?,?,?)',
            (operation_id, input_hash, canonical_json(result), now),
        )
