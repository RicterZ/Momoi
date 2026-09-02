import json
import logging
import time
from typing import Any

from ...logging_context import TRACE, log_event
from ...storage import estimate_tokens
from ..turn_support import TurnBudgetExceeded, truncate_tool_result_json

logger = logging.getLogger("momoi.runtime.turns")
MAX_TOOL_RESULT_TRUNCATION_ATTEMPTS = 16


def context_compaction_tokens(config: Any) -> int:
    return max(
        1,
        round(
            config.max_input_tokens
            * float(getattr(config, "context_compaction_ratio", 1.0))
        ),
    )


class ContextWindow:
    """Applies per-Turn budgets and fits requests into the model window."""

    def __init__(self, config: Any, store: Any, tool_results: Any):
        self.config = config
        self.store = store
        self.tool_results = tool_results

    def check_budget(
        self, turn_id: str, system: object, messages: object, tools: object
    ) -> None:
        usage = self.store.turn_usage(turn_id)
        elapsed = time.time() - float(usage["started_at"])
        if self.config.turn_max_seconds and elapsed >= self.config.turn_max_seconds:
            raise TurnBudgetExceeded("time limit reached")
        estimated_input = estimate_tokens(
            json.dumps(
                {"system": system, "messages": messages, "tools": tools},
                ensure_ascii=False,
                default=str,
            )
        )
        total = int(usage["input"]) + int(usage["output"])
        if (
            self.config.turn_max_total_tokens
            and total + estimated_input > self.config.turn_max_total_tokens
        ):
            raise TurnBudgetExceeded("token limit reached")

    def fit(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        history_messages: int,
    ) -> int:
        def size() -> int:
            return estimate_tokens(
                json.dumps(
                    {"system": system, "messages": messages, "tools": tools},
                    ensure_ascii=False,
                    default=str,
                )
            )

        hard_limit = self.config.max_input_tokens
        compaction_limit = min(hard_limit, context_compaction_tokens(self.config))
        estimated = size()
        dropped = 0
        truncated = 0
        compression_breakers = 0
        while estimated > compaction_limit and history_messages:
            messages.pop(0)
            history_messages -= 1
            dropped += 1
            while history_messages and str(messages[0].get("role")) == "assistant":
                messages.pop(0)
                history_messages -= 1
                dropped += 1
            estimated = size()
        if estimated > compaction_limit:
            for message in messages:
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        estimated <= compaction_limit
                        or not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    result = block.get("content")
                    attempts = 0
                    while (
                        isinstance(result, str)
                        and len(result) > 1000
                        and estimated > compaction_limit
                    ):
                        if attempts >= MAX_TOOL_RESULT_TRUNCATION_ATTEMPTS:
                            compression_breakers += 1
                            log_event(
                                logger,
                                logging.WARNING,
                                "tool_result_truncation_stalled",
                                reason="attempt_limit",
                                attempts=attempts,
                                result_chars=len(result),
                                estimated_input=estimated,
                                input_limit=compaction_limit,
                            )
                            break
                        attempts += 1
                        target = max(1000, len(result) // 2)
                        candidate = self.tool_results.refit(
                            result, max_chars=target
                        ) or truncate_tool_result_json(result, target)
                        if len(candidate) >= len(result):
                            compression_breakers += 1
                            log_event(
                                logger,
                                logging.WARNING,
                                "tool_result_truncation_stalled",
                                reason="non_shrinking_result",
                                attempts=attempts,
                                before_chars=len(result),
                                after_chars=len(candidate),
                                estimated_input=estimated,
                                input_limit=compaction_limit,
                            )
                            break
                        before_estimated = estimated
                        block["content"] = candidate
                        candidate_estimated = size()
                        if candidate_estimated >= before_estimated:
                            block["content"] = result
                            compression_breakers += 1
                            log_event(
                                logger,
                                logging.WARNING,
                                "tool_result_truncation_stalled",
                                reason="non_shrinking_input",
                                attempts=attempts,
                                before_chars=len(result),
                                after_chars=len(candidate),
                                before_estimated=before_estimated,
                                after_estimated=candidate_estimated,
                                input_limit=compaction_limit,
                            )
                            break
                        result = candidate
                        estimated = candidate_estimated
                        truncated += 1
        log_event(
            logger,
            TRACE,
            "llm_context_fit",
            estimated_input=estimated,
            compaction_limit=compaction_limit,
            input_limit=hard_limit,
            history_dropped=dropped,
            tool_results_truncated=truncated,
            compression_breakers=compression_breakers,
        )
        if estimated > hard_limit:
            log_event(
                logger,
                logging.WARNING,
                "llm_context_oversize",
                estimated_input=estimated,
                input_limit=hard_limit,
                single_turn_context=history_messages == 0,
                proceeding=True,
                history_dropped=dropped,
                compression_breakers=compression_breakers,
            )
        return history_messages
