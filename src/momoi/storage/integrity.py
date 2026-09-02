from __future__ import annotations

import copy
import json
import logging
from typing import TypeVar, cast

from ..observability.events import log_event


logger = logging.getLogger(__name__)
T = TypeVar("T")
_RAISE = object()


class StorageIntegrityError(ValueError):
    pass


def decode_stored_json(
    value: object,
    *,
    entity: str,
    record_id: object,
    field: str,
    expected_type: type[T] | tuple[type, ...],
    fallback: T | object = _RAISE,
) -> T:
    try:
        parsed = json.loads(str(value))
        if not isinstance(parsed, expected_type):
            expected = (
                "/".join(item.__name__ for item in expected_type)
                if isinstance(expected_type, tuple)
                else expected_type.__name__
            )
            raise TypeError(f"expected {expected}, got {type(parsed).__name__}")
        return cast(T, parsed)
    except (TypeError, json.JSONDecodeError) as error:
        log_event(
            logger,
            logging.ERROR,
            "storage_integrity_error",
            entity=entity,
            record_id=str(record_id),
            field=field,
            error_type=type(error).__name__,
        )
        if fallback is _RAISE:
            raise StorageIntegrityError(
                f"corrupt {entity} {record_id} field {field}"
            ) from error
        return copy.deepcopy(fallback)  # type: ignore[return-value]
