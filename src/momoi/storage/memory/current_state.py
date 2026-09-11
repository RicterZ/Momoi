"""Transactional TTL slots, independent of prompts, model calls and turn scheduling.

Callers supply an operation ID for retries and the snapshot revision for CAS.
Expiry never extends a slot's lifetime. History stores inverse changes so a
future chat-cleanup integration can restore an earlier revision without guessing.
"""

from collections.abc import Callable, Sequence
import copy
from dataclasses import asdict, dataclass
import json
import math
import re
import sqlite3
import time
import uuid

from .current_state_contract import (
    CURRENT_STATE_CHANGE_SCHEMA,
    ID_MAX_LENGTH,
    KEY_MAX_LENGTH,
    KEY_PATTERN,
    MAX_SLOTS,
    MAX_TTL_SECONDS,
    SUBJECT_MAX_LENGTH,
    VALUE_MAX_LENGTH,
)


class StateConflict(ValueError):
    """Stale snapshot, mismatched retry, or conflicting state dimensions."""


@dataclass(frozen=True)
class SlotInput:
    subject: str
    key: str
    value: str
    ttl_seconds: int


@dataclass(frozen=True)
class StateSlot:
    id: str
    subject: str
    key: str
    value: str
    created_at: float
    expires_at: float
    source_turn_id: str


@dataclass(frozen=True)
class StateSnapshot:
    revision: int
    slots: tuple[StateSlot, ...]


@dataclass(frozen=True)
class StateChange:
    revision: int
    operation_id: str
    source_turn_id: str
    created_at: float
    added: tuple[StateSlot, ...]
    removed: tuple[StateSlot, ...]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid_{name}")
    return value.strip()


class CurrentStateManager:
    MAX_TTL_SECONDS = MAX_TTL_SECONDS
    MAX_SLOTS = MAX_SLOTS

    def __init__(
        self, database: sqlite3.Connection, *, clock: Callable[[], float] = time.time
    ):
        self._db = database
        self._clock = clock

    def _now(self):
        now = self._clock()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
        ):
            raise ValueError("invalid_clock")
        return float(now)

    def snapshot(self) -> StateSnapshot:
        """Expired rows never appear, even if physical expiry has not run."""
        rows = self._db.execute(
            """SELECT r.revision,s.* FROM current_state_revision r
               LEFT JOIN current_state_slots s ON s.expires_at>?
               WHERE r.id=1 ORDER BY s.subject,s.key,s.id""",
            (self._now(),),
        ).fetchall()
        return StateSnapshot(
            rows[0][0],
            tuple(StateSlot(*tuple(row)[1:]) for row in rows if row[1] is not None),
        )

    @classmethod
    def change_schema(cls) -> dict:
        """Detached schema for callers; mutation does not alter the shared contract."""
        schema = copy.deepcopy(CURRENT_STATE_CHANGE_SCHEMA)
        schema["properties"]["add"]["maxItems"] = cls.MAX_SLOTS
        schema["properties"]["delete"]["maxItems"] = cls.MAX_SLOTS
        schema["properties"]["add"]["items"]["properties"]["ttl_seconds"]["maximum"] = (
            cls.MAX_TTL_SECONDS
        )
        return schema

    def apply_arguments(
        self,
        arguments: object,
        *,
        source_turn_id: str,
        operation_id: str,
        expected_revision: int,
    ) -> StateChange:
        """Consume the schema's JSON shape, keeping runtime metadata caller-owned."""
        if not isinstance(arguments, dict) or set(arguments) != {"add", "delete"}:
            raise ValueError("invalid_change_set")
        added, deleted = arguments["add"], arguments["delete"]
        if not isinstance(added, list) or not isinstance(deleted, list):
            raise ValueError("invalid_change_set")
        fields = set(
            CURRENT_STATE_CHANGE_SCHEMA["properties"]["add"]["items"]["required"]
        )
        if any(not isinstance(item, dict) or set(item) != fields for item in added):
            raise ValueError("invalid_slot_input")
        return self.apply(
            add=[SlotInput(**item) for item in added],
            delete=deleted,
            source_turn_id=source_turn_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
        )

    @staticmethod
    def _change(row) -> StateChange:
        revision, operation_id, _request, source, at, added, removed = tuple(row)
        return StateChange(
            revision,
            operation_id,
            source,
            at,
            tuple(StateSlot(**value) for value in json.loads(added)),
            tuple(StateSlot(**value) for value in json.loads(removed)),
        )

    def history(self, *, after_revision: int = 0) -> tuple[StateChange, ...]:
        self._revision_value(after_revision)
        return tuple(
            self._change(row)
            for row in self._db.execute(
                "SELECT * FROM current_state_changes WHERE revision>? ORDER BY revision",
                (after_revision,),
            )
        )

    @staticmethod
    def _revision_value(value):
        if type(value) is not int or value < 0:
            raise ValueError("invalid_revision")

    def apply(
        self,
        *,
        add: Sequence[SlotInput] = (),
        delete: Sequence[str] = (),
        source_turn_id: str,
        operation_id: str,
        expected_revision: int,
    ) -> StateChange:
        """Apply a model-independent change set. Replacements require delete + add.

        Unknown deletion IDs and duplicate dimensions reject the whole operation.
        Expired slots are removed in the same transaction before capacity checks.
        """
        if (
            isinstance(delete, (str, bytes))
            or len(add) > self.MAX_SLOTS
            or len(delete) > self.MAX_SLOTS
        ):
            raise ValueError("invalid_change_set")
        inputs = []
        for item in add:
            if not isinstance(item, SlotInput):
                raise ValueError("invalid_slot_input")
            subject = _text(item.subject, "subject", SUBJECT_MAX_LENGTH)
            key = _text(item.key, "key", KEY_MAX_LENGTH)
            if re.fullmatch(KEY_PATTERN, item.key) is None:
                raise ValueError("invalid_key")
            value = _text(item.value, "value", VALUE_MAX_LENGTH)
            if (
                type(item.ttl_seconds) is not int
                or not 1 <= item.ttl_seconds <= self.MAX_TTL_SECONDS
            ):
                raise ValueError("invalid_ttl")
            inputs.append(asdict(SlotInput(subject, key, value, item.ttl_seconds)))
        ids = [_text(value, "slot_id", ID_MAX_LENGTH) for value in delete]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_delete")
        return self._mutate(
            {"kind": "apply", "add": inputs, "delete": ids},
            source_turn_id,
            operation_id,
            expected_revision,
        )

    def expire(self, *, operation_id: str, expected_revision: int) -> StateChange:
        """Physically remove expired slots, recording their original expiry."""
        return self._mutate(
            {"kind": "expire"}, "runtime:expiry", operation_id, expected_revision
        )

    def restore(
        self, revision: int, *, operation_id: str, expected_revision: int
    ) -> StateChange:
        """Restore an earlier snapshot as a new change; never resurrect expired slots.

        This is an explicit API only, not connected to chat deletion. The audit
        history is retained; callers deleting private data must handle it too.
        """
        self._revision_value(revision)
        return self._mutate(
            {"kind": "restore", "revision": revision},
            "runtime:restore",
            operation_id,
            expected_revision,
        )

    def _mutate(self, request, source, operation_id, expected_revision):
        self._revision_value(expected_revision)
        source = _text(source, "source_turn_id", 128)
        operation_id = _text(operation_id, "operation_id", 128)
        encoded = _json(
            {
                **request,
                "source_turn_id": source,
                "expected_revision": expected_revision,
            }
        )
        if self._db.in_transaction:
            raise RuntimeError("current_state_requires_own_transaction")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self._db.execute(
                "SELECT * FROM current_state_changes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if existing[2] != encoded:
                    raise StateConflict("operation_id_reused")
                return self._change(existing)
            revision = self._db.execute(
                "SELECT revision FROM current_state_revision WHERE id=1"
            ).fetchone()[0]
            if revision != expected_revision:
                raise StateConflict("stale_revision")
            now = self._now()
            before = {
                row[0]: StateSlot(*tuple(row))
                for row in self._db.execute(
                    "SELECT * FROM current_state_slots ORDER BY id"
                )
            }
            after = {key: slot for key, slot in before.items() if slot.expires_at > now}
            if request["kind"] == "restore":
                target = request["revision"]
                if target > revision:
                    raise ValueError("future_revision")
                after = dict(before)
                for change in reversed(self.history(after_revision=target)):
                    for slot in change.added:
                        after.pop(slot.id, None)
                    after.update({slot.id: slot for slot in change.removed})
                after = {
                    key: slot for key, slot in after.items() if slot.expires_at > now
                }
            elif request["kind"] == "apply":
                for key in request["delete"]:
                    if key not in before:
                        raise StateConflict("slot_not_found")
                    after.pop(key, None)
                dimensions = {(slot.subject, slot.key) for slot in after.values()}
                for item in request["add"]:
                    dimension = (item["subject"], item["key"])
                    if dimension in dimensions:
                        raise StateConflict("slot_dimension_exists")
                    dimensions.add(dimension)
                    slot = StateSlot(
                        uuid.uuid4().hex,
                        item["subject"],
                        item["key"],
                        item["value"],
                        now,
                        now + item["ttl_seconds"],
                        source,
                    )
                    after[slot.id] = slot
            if len(after) > self.MAX_SLOTS:
                raise ValueError(
                    f"slot_capacity_exceeded: at most {self.MAX_SLOTS} slots; "
                    "delete ended or weaker slots in the same change set, then resubmit"
                )
            removed = tuple(slot for key, slot in before.items() if key not in after)
            added = tuple(slot for key, slot in after.items() if key not in before)
            self._db.executemany(
                "DELETE FROM current_state_slots WHERE id=?",
                ((slot.id,) for slot in removed),
            )
            self._db.executemany(
                "INSERT INTO current_state_slots VALUES (?,?,?,?,?,?,?)",
                (tuple(asdict(slot).values()) for slot in added),
            )
            self._db.execute(
                "UPDATE current_state_revision SET revision=? WHERE id=1",
                (revision + 1,),
            )
            self._db.execute(
                "INSERT INTO current_state_changes VALUES (?,?,?,?,?,?,?)",
                (
                    revision + 1,
                    operation_id,
                    encoded,
                    source,
                    now,
                    _json([asdict(slot) for slot in added]),
                    _json([asdict(slot) for slot in removed]),
                ),
            )
            return StateChange(revision + 1, operation_id, source, now, added, removed)
