"""Fail-closed affinity review using real delivered conversation."""
import asyncio
import json
from importlib.resources import files

from ...integrations.request_context import model_request
from ...memory.text import estimate_tokens
from ...observability.context import log_context, new_trace_id
from ...tools.validation import validate_tool_arguments
from ..tool_contracts.affinity import AFFINITY_TOOLS
from .replyer import prompt_text


async def review_affinity(config, store, provider, call, request):
    if not getattr(config, "affinity_enabled", False):
        return {"ok": False, "allowed": False, "reason": "好感度未开启。"}
    spec = next(spec for spec in AFFINITY_TOOLS if spec["name"] == call.name)
    args, error = validate_tool_arguments(call.name, call.arguments, spec["input_schema"])
    if error:
        return {**error, "allowed": False, "reason": "只接受非空理由和证据，每次固定调整 1 点。"}
    if not args["reason"].strip() or not args["evidence"].strip():
        return {"ok": False, "allowed": False, "reason": "理由和证据不能留空。"}
    args = {**args, "amount": 1}
    current = store.self_state()["affinity"]
    delta = 1 if call.name == "affinity_increase" else -1
    if not 0 <= current + delta <= 100:
        return {"ok": False, "allowed": False, "reason": "调整后好感度必须在 0–100 之间。", "value": current}
    rows = store.replyer_dialogue_rows(request.delivery_channel.name, limit=40)
    if len(rows) < 10:
        return {"ok": False, "allowed": False, "reason": "真实对话不足 10 条，暂不能审批。", "value": current}
    rules = prompt_text(config, "AFFINITY.md", files("momoi").joinpath("prompts/affinity.md").read_text(encoding="utf-8"))
    payload = {"current_affinity": current, "direction": call.name, **args,
               "dialogue": [{key: row[key] for key in ("id", "role", "content", "created_at")} for row in rows],
               "previous_reviews": store.affinity_reviews()}
    try:
        with log_context(stage="affinity_checker", turn_id=request.turn_id, call_id=new_trace_id()), model_request():
            async with asyncio.timeout(60):
                response = await provider.complete(
                    [{"type": "text", "text": files("momoi").joinpath("prompts/affinity_checker.md").read_text(encoding="utf-8").replace("{{AFFINITY}}", rules)}],
                    [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], [], require_tool=False,
                )
        usage = response.usage or {}
        store.record_turn_usage(request.turn_id, int(usage.get("input", estimate_tokens(str(payload)))),
                                int(usage.get("output", estimate_tokens(str(response.content)))))
        if response.tool_calls:
            raise ValueError("approval must return JSON, not tools")
        text = "\n".join(block.get("text", "") for block in response.content if block.get("type") == "text")
        decision = json.loads(text)
        if (not isinstance(decision, dict) or set(decision) != {"allowed", "reason"}
                or type(decision["allowed"]) is not bool or not isinstance(decision["reason"], str)
                or not decision["reason"].strip()):
            raise ValueError("invalid review")
    except Exception as error:
        return {"ok": False, "allowed": False, "reason": "审批失败或返回格式无效，好感度未修改。", "error": type(error).__name__}
    if not getattr(config, "affinity_enabled", False):
        return {"ok": False, "allowed": False, "reason": "审批期间好感度已关闭，未修改。"}
    return store.apply_affinity_review(request.turn_id, current, delta, args, decision)
