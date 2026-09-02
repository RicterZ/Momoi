import logging
from typing import Any

from .context import current_log_context

TRACE = 5
logging.addLevelName(TRACE, "TRACE")
logging.TRACE = TRACE


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    message: str | None = None,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    logger.log(
        level,
        message or event,
        exc_info=exc_info,
        extra={
            "momoi_event": event,
            "momoi_context": current_log_context(),
            "momoi_fields": {
                key: value for key, value in fields.items() if value is not None
            },
        },
    )
