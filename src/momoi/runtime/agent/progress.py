import copy
from typing import Any

from ...contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD


def requires_owner_progress(spec: dict[str, Any]) -> bool:
    return spec.get(OWNER_PROGRESS_FIELD) == OWNER_PROGRESS_BEFORE_FIRST_CALL


def public_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    public = copy.deepcopy(spec)
    public.pop(OWNER_PROGRESS_FIELD, None)
    return public
