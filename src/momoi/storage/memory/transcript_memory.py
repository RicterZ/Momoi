"""Durable memory snapshot and append-only changes for the shared transcript."""
import json
import time
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from ...memory.storage.transactions import transaction
from .presentation import format_memory


class TranscriptMemoryStore:
    def retained_transcript_rows(self, rows):
        row = self._db.execute(
            "SELECT data_json FROM transcript_memory_state WHERE id=1"
        ).fetchone()
        excluded = set(json.loads(row[0]).get("dropped_turn_ids", [])) if row else set()
        return [row for row in rows if str(row["turn_id"]) not in excluded]

    def transcript_episode_snapshot(self, content):
        """Freeze the episode prefix until the next shared compaction."""
        with transaction(self._db):
            row = self._db.execute(
                "SELECT data_json FROM transcript_memory_state WHERE id=1"
            ).fetchone()
            if not row:
                return content
            state = json.loads(row[0])
            state.setdefault("episode_snapshot", content)
            self._db.execute(
                "UPDATE transcript_memory_state SET data_json=? WHERE id=1",
                (json.dumps(state, ensure_ascii=False),),
            )
            return state["episode_snapshot"]

    def persist_transcript_compaction(self, dropped_turn_ids, episode_content):
        with transaction(self._db):
            row = self._db.execute(
                "SELECT data_json FROM transcript_memory_state WHERE id=1"
            ).fetchone()
            if not row:
                return
            state = json.loads(row[0])
            state["dropped_turn_ids"] = sorted(
                set(state.get("dropped_turn_ids", [])) | set(dropped_turn_ids)
            )
            if episode_content is not None:
                state["episode_snapshot"] = episode_content
            # Prevent boundary tracking from folding the same compaction again.
            state["boundary"] = ""
            self._db.execute(
                "UPDATE transcript_memory_state SET data_json=? WHERE id=1",
                (json.dumps(state, ensure_ascii=False),),
            )

    def _only_always_memory_changes(self, state):
        """Discard legacy recall/scoped deltas without resetting revision history."""
        tracked = {str(key) for key, row in state.get("snapshot", {}).items()
                   if row.get("activation") == "always"}
        state["snapshot"] = {key: row for key, row in state.get("snapshot", {}).items()
                             if row.get("activation") == "always"}
        retained = []
        overrides = {}
        for event in state.get("events", []):
            try:
                root = ElementTree.fromstring(event["content"])
            except (KeyError, ElementTree.ParseError):
                continue
            changed = False
            for node in list(root):
                identifier = node.get("id")
                memory = node.find("memory")
                is_always = memory is not None and memory.get("activation") == "always"
                if is_always:
                    tracked.add(identifier)
                elif node.tag == "delete" and identifier in tracked:
                    tracked.remove(identifier)
                elif identifier in tracked:
                    tracked.remove(identifier)
                    node.tag = "delete"
                    node.clear()
                    node.set("id", identifier)
                    node.text = "此长期记忆已撤销，不再作为当前依据。"
                    changed = True
                else:
                    root.remove(node)
                    changed = True
                    continue
                if changed:
                    overrides[identifier] = ElementTree.tostring(node, encoding="unicode")
            if len(root):
                retained.append({**event, "content": ElementTree.tostring(root, encoding="unicode") if changed else event["content"]})
        state["events"] = retained
        state["observed"] = {key: row for key, row in state.get("observed", {}).items()
                             if row.get("activation") == "always"}
        # Older states can have folded deltas that no longer appear in events.
        override_ids = set(state.get("overrides", {})) | set(state.get("snapshot_overrides", {}))
        historical_always = set()
        if override_ids:
            placeholders = ",".join("?" for _ in override_ids)
            historical_always = {str(row["id"]) for row in self._db.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders}) AND activation='always'",
                tuple(override_ids),
            )}
        for field in ("overrides", "snapshot_overrides"):
            state[field] = {key: value for key, value in state.get(field, {}).items()
                            if key in state["snapshot"] or key in state["observed"]
                            or key in historical_always}
        state["overrides"].update(overrides)

    def transcript_memory_context(self, turn_ids, *, compact=False, track_boundary=True):
        """Observe committed effective memory, retaining the prefix until compaction.

        Compare the complete effective inventory so dashboard edits, tombstones,
        supersession, expiry and background review share the same path.
        """
        with transaction(self._db):
            current = {
                str(row["id"]): dict(row)
                for row in self.memories.repository.inventory()
                if row["activation"] == "always"
            }
            raw = self._db.execute(
                "SELECT data_json FROM transcript_memory_state WHERE id=1"
            ).fetchone()
            state = json.loads(raw[0]) if raw else None
            if state is not None:
                self._only_always_memory_changes(state)
                folded = state.get("snapshot_overrides", {})
                state.setdefault("folded_overrides", {}).update(folded)
                state["snapshot_overrides"] = {}
                state["overrides"] = {k: v for k, v in state.get("overrides", {}).items()
                                      if folded.get(k) != v}
            boundary = turn_ids[0] if turn_ids else ""
            if track_boundary and state and state.pop("pending_compact", False):
                compact = True
            if track_boundary and state and state["boundary"] and boundary != state["boundary"]:
                compact = True
            if state is None:
                state = {"revision": 0, "snapshot_revision": 0, "snapshot": current, "observed": current,
                         "history_format": 4, "events": [], "boundary": boundary, "overrides": {}, "snapshot_overrides": {}}
            else:
                state["history_format"] = 4
                previous = state["observed"]
                changes = []
                for identifier in sorted(previous.keys() | current.keys(), key=int):
                    old, new = previous.get(identifier), current.get(identifier)
                    # Only semantic fields affect LLM context, not access/ranking timestamps.
                    fields = ("kind", "key", "content", "activation")
                    if old and new and all(old.get(k) == new.get(k) for k in fields):
                        continue
                    if old is None and new is None:
                        continue
                    state["revision"] += 1
                    operation = "delete" if new is None else ("add" if old is None else "replace")
                    body = (
                        format_memory(new) if new else
                        "此记忆已撤销。旧快照及历史检索中的同 ID 内容不再作为当前事实或偏好依据。"
                    )
                    changes.append(
                        f'<{operation} id={quoteattr(identifier)} revision="{state["revision"]}"'
                        f' key={quoteattr(str((new or old)["key"]))}>'
                        f'{body}</{operation}>'
                    )
                    if old is not None or identifier in state["overrides"]:
                        state.setdefault("overrides", {})[identifier] = changes[-1]
                if changes:
                    state["events"].append({
                        "anchor": turn_ids[-1] if turn_ids else "",
                        "revision": state["revision"],
                        "content": '<memory_changes at=' + quoteattr(self.context_timestamp(time.time()))
                        + '>\n' + "\n".join(changes) + '\n</memory_changes>',
                    })
                state["observed"] = current
            if compact:
                state.pop("episode_snapshot", None)
                state["history_format"] = 4
                state["snapshot_revision"] = state["revision"]
                state["snapshot"] = current
                state.setdefault("folded_overrides", {}).update(state["overrides"])
                state["overrides"] = {}
                state["snapshot_overrides"] = {}
                state["events"] = []
            if track_boundary:
                state["boundary"] = boundary
            self._db.execute(
                "INSERT INTO transcript_memory_state(id,data_json) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
                (json.dumps(state, ensure_ascii=False),),
            )
        return state

    def fold_transcript_memory(self, revision):
        """Fold only a request's observed revision; never consume concurrent updates."""
        with transaction(self._db):
            row = self._db.execute(
                "SELECT data_json FROM transcript_memory_state WHERE id=1"
            ).fetchone()
            if not row:
                return
            state = json.loads(row[0])
            if state["revision"] != revision:
                return
            state["history_format"] = 4
            state["snapshot_revision"] = state["revision"]
            state["snapshot"] = state["observed"]
            state.setdefault("folded_overrides", {}).update(state["overrides"])
            state["overrides"] = {}
            state["snapshot_overrides"] = {}
            state["events"] = []
            self._db.execute(
                "UPDATE transcript_memory_state SET data_json=? WHERE id=1",
                (json.dumps(state, ensure_ascii=False),),
            )
