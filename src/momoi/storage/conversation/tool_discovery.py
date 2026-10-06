"""Enabled tools belong to the shared transcript's compaction cycle."""
TOOL_DISCOVERY_SCHEMA = """CREATE TABLE IF NOT EXISTS transcript_enabled_tools (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL
)"""


class ToolDiscoveryStore:
    def transcript_enabled_tools(self):
        return [row[0] for row in self._db.execute(
            "SELECT name FROM transcript_enabled_tools ORDER BY position, name"
        )]

    def enable_transcript_tools(self, names):
        with self._db:
            position = self._db.execute(
                "SELECT COALESCE(MAX(position), -1) FROM transcript_enabled_tools"
            ).fetchone()[0]
            for name in names:
                position += 1
                self._db.execute(
                    "INSERT OR IGNORE INTO transcript_enabled_tools(name, position) VALUES (?, ?)",
                    (name, position),
                )
