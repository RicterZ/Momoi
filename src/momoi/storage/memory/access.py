"""Turn-atomic access accounting and version-aware read-only aggregates."""
import time

from ...memory.storage.transactions import transaction
from ...memory.storage.records import LIVE_MEMORY_SQL

ACCESS_KINDS = ("recalled", "searched", "browsed", "read", "always")
DAY = 86400


class MemoryAccessStore:
    def memory_access_summary(self, *, now=None):
        now = time.time() if now is None else now
        ids = [row[0] for row in self._db.execute(
            f"SELECT id FROM memories m WHERE {LIVE_MEMORY_SQL}", (now,),
        )]
        stats = self.memory_access_stats(ids, now=now)
        calls = {name: {"total": 0, "recent_30_days": 0}
                 for name in ("memory_search", "recall")}
        for row in self._db.execute(
            """SELECT tool_name,COUNT(*) AS count,SUM(started_at>=?) AS recent
               FROM tool_audit WHERE tool_name IN ('memory_search','recall')
               GROUP BY tool_name""", (now - 30 * DAY,),
        ):
            calls[row["tool_name"]] = {"total": row["count"], "recent_30_days": row["recent"]}
        return {"memories": len(ids), "unused_memories": sum(item["total"] == 0 for item in stats.values()),
                "tool_calls": calls}

    def _commit_memory_access(self, turn_id, accesses):
        """Caller owns the Turn transaction; recording never commits separately."""
        if not accesses:
            return
        self._db.executemany(
            "INSERT OR IGNORE INTO memory_reply_usage(memory_id,turn_id,event_key,created_at) VALUES(?,?,?,?)",
            ((item['memory_id'], turn_id, item['event_key'], item['created_at'])
             for item in accesses if item['kind'] == 'used'),
        )
        accesses = [item for item in accesses if item['kind'] != 'used']
        workflow = self.turn_workflow_kind(turn_id) or "owner"
        prefixes = {item["event_key"] for item in accesses if item["kind"] == "always"}
        accepted = {generation for generation in prefixes if self._db.execute(
            "INSERT OR IGNORE INTO memory_access_prefixes(generation) VALUES(?)", (generation,)
        ).rowcount}
        self._db.executemany(
            """INSERT OR IGNORE INTO memory_access
               (memory_id,kind,turn_id,workflow,event_key,created_at) VALUES(?,?,?,?,?,?)""",
            ((item["memory_id"], item["kind"], turn_id, workflow, item["event_key"], item["created_at"])
             for item in accesses if item["kind"] != "always" or item["event_key"] in accepted),
        )
        self._compact_memory_access(time.time())

    def _compact_memory_access(self, now):
        cutoff = now - 180 * DAY
        self._db.execute(
            """INSERT INTO memory_access_totals(memory_id,kind,count,last_at)
               SELECT memory_id,kind,COUNT(*),MAX(created_at) FROM memory_access
               WHERE created_at < ? GROUP BY memory_id,kind
               ON CONFLICT(memory_id,kind) DO UPDATE SET
                 count=count+excluded.count, last_at=MAX(last_at,excluded.last_at)""", (cutoff,),
        )
        self._db.execute("DELETE FROM memory_access WHERE created_at < ?", (cutoff,))
        self._db.execute("DELETE FROM memory_reply_usage WHERE created_at < ?", (cutoff,))

    def compact_memory_access(self, *, now=None):
        with transaction(self._db):
            self._compact_memory_access(time.time() if now is None else now)

    def memory_access_stats(self, memory_ids, *, now=None):
        """Include prior versions and merged sources without crossing scope identities."""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        now = time.time() if now is None else now
        result = {identifier: {
            "counts": dict.fromkeys(ACCESS_KINDS, 0), "total": 0,
            "last_at": None, "recent_30_days": 0,
        } for identifier in ids}
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"""WITH RECURSIVE lineage(root,id) AS (
                SELECT m.id,v.id FROM memories m JOIN memories v
                  ON m.kind=v.kind AND m.key=v.key AND m.scope_key=v.scope_key
                WHERE m.id IN ({placeholders})
                UNION
                SELECT l.root,v.id FROM lineage l JOIN memories v ON v.superseded_by=l.id
            ), usage AS (
                SELECT memory_id,kind,COUNT(*) AS count,MAX(created_at) AS last_at,
                       SUM(created_at>=?) AS recent FROM memory_access GROUP BY memory_id,kind
                UNION ALL
                SELECT memory_id,kind,count,last_at,0 FROM memory_access_totals
            )
            SELECT l.root,u.kind,SUM(u.count) AS count,MAX(u.last_at) AS last_at,
                   SUM(u.recent) AS recent FROM lineage l JOIN usage u ON u.memory_id=l.id
            GROUP BY l.root,u.kind""", (*ids, now - 30 * DAY),
        ).fetchall()
        for row in rows:
            item = result[row["root"]]
            item["counts"][row["kind"]] = row["count"]
            item["total"] += row["count"]
            item["recent_30_days"] += row["recent"]
            item["last_at"] = max(item["last_at"] or 0, row["last_at"])
        return result
