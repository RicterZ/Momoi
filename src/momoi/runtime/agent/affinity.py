"""Fail-closed affinity review using real delivered conversation."""
import asyncio
import json
from importlib.resources import files

from ...integrations.request_context import model_request
from ...memory.text import estimate_tokens
from ...observability.context import log_context, new_trace_id, current_log_context
from ...tools.validation import validate_tool_arguments
from ..tool_contracts.affinity import AFFINITY_TOOLS
from .replyer import prompt_text


def affinity_review_message(current, direction, arguments, rows, previous):
    stage = next(label for maximum, label in (
        (9, "疏离与抵触"), (19, "陌生与客气"), (29, "熟悉"), (39, "朋友"),
        (69, "好朋友"), (89, "亲密与恋爱"), (100, "深度承诺")) if current <= maximum)

    def dialogue(messages):
        return "\n".join(f"{row['role'].upper()}: {row['content']}" for row in messages)

    lines = [f"申请{'增加' if direction == 'increase' else '降低'}好感度，当前关系阶段为：{stage}。",
             f"申请的理由是：{arguments['reason']}",
             "申请对象为聊天中的 ASSISTANT，申请调整的是 ASSISTANT 对 USER 的好感度。",
             f"申请提供的证据：{arguments['evidence']}",
             "聊天证据如下：", "```", dialogue(rows), "```",
             "此前的理由和证据仅作历史参考，不替代或预先决定本次判断；仅用于核对是否重复计分。"]
    approved = [review for review in previous if review.get("allowed")]
    if not approved:
        lines.append("此前没有已允许的好感度变动记录。")
    for review in approved:
        lines.extend([f"此前，好感度变动的理由是：{review['planner_reason']}",
                      f"之前审批器允许调整的理由是：{review['reason']}",
                      f"之前申请提供的证据：{review['evidence']}",
                      "之前的聊天证据如下：", "```",
                      dialogue(review.get("dialogue", [])) or "无已保存的聊天证据。", "```"])
    return "\n\n".join(lines)


async def review_affinity(config, store, provider, call, request):
    if not getattr(config, "affinity_enabled", False):
        return {"ok": False, "allowed": False, "reason": "好感度未开启。"}
    spec = next(spec for spec in AFFINITY_TOOLS if spec["name"] == call.name)
    args, error = validate_tool_arguments(call.name, call.arguments, spec["input_schema"])
    if error:
        return {**error, "allowed": False, "reason": "只接受非空理由和证据，不接受幅度参数。"}
    if not args["reason"].strip():
        return {"ok": False, "allowed": False, "reason": "理由不能留空。"}
    if args["direction"] == "unchanged":
        if not getattr(request, "affinity_settlement", False):
            return {"ok": False, "allowed": False, "reason": "只有每日复盘可选择保持不变。"}
        return {"ok": True, "state": "unchanged", "reason": args["reason"]}
    if not args["reason"].strip() or not args["evidence"].strip():
        return {"ok": False, "allowed": False, "reason": "理由和证据不能留空。"}
    args = {**args, "amount": 1}
    current = store.self_state()["affinity"]
    delta = 1 if args["direction"] == "increase" else -1
    if not 0 <= current + delta <= 100:
        return {"ok": False, "allowed": False, "reason": "调整后好感度必须在 0–100 之间。"}
    rows = getattr(request, "affinity_dialogue", None)
    if rows is None:
        rows = store.replyer_dialogue_rows(request.delivery_channel.name, limit=40)
    if len(rows) < 10:
        return {"ok": False, "allowed": False, "reason": "真实对话不足 10 条，暂不能审批。"}
    rules = prompt_text(config, "AFFINITY.md", files("momoi").joinpath("prompts/affinity.md").read_text(encoding="utf-8"))
    message = affinity_review_message(current, args["direction"], args, rows, store.affinity_reviews())
    try:
        parent_call_id = current_log_context().get("call_id", getattr(request, "call_id", ""))
        with log_context(stage="affinity_checker", turn_id=request.turn_id, call_id=new_trace_id(),
                         parent_call_id=parent_call_id, tool_call_id=getattr(call, "id", ""),
                         round=getattr(request, "round_number", 0), channel=request.delivery_channel.name), model_request():
            async with asyncio.timeout(60):
                response = await provider.complete(
                    [{"type": "text", "text": files("momoi").joinpath("prompts/affinity_checker.md").read_text(encoding="utf-8").replace("{{AFFINITY}}", rules)}],
                    [{"role": "user", "content": message}], [], require_tool=False,
                )
        usage = response.usage or {}
        store.record_turn_usage(request.turn_id, int(usage.get("input", estimate_tokens(message))),
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
    result = store.apply_affinity_review(request.turn_id, current, delta, {**args, "dialogue": rows, "settlement": bool(getattr(request, "affinity_settlement", False)), "settlement_date": getattr(request, "affinity_settlement_date", None)}, decision)
    return {key: value for key, value in result.items() if key != "value"}
