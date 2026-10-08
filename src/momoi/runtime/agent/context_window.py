import json
import logging
import time
from typing import Any

from ...observability.events import TRACE, log_event
from ...storage import estimate_tokens
from ..context.presentation import recent_episode_lines
from ..turn_support import (
    TurnBudgetExceeded,
    context_data_message,
    truncate_tool_result_json,
)

logger = logging.getLogger("momoi.runtime.turns")
MAX_TOOL_RESULT_TRUNCATION_ATTEMPTS = 16


def _without_embedded_image_data(value: Any) -> Any:
    """Remove transport-only image bytes from context token estimates."""
    if isinstance(value, list):
        return [_without_embedded_image_data(item) for item in value]
    if not isinstance(value, dict):
        return value

    embedded_source = value.get("type") == "base64" and isinstance(
        value.get("data"), str
    )
    sanitized: dict[Any, Any] = {}
    for key, item in value.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if embedded_source and key == "data":
            sanitized[key] = ""
        elif (
            key in {"url", "file"}
            and isinstance(item, str)
            and item.startswith("data:image/")
            and ";base64," in item[:100]
        ):
            sanitized[key] = item.split(";base64,", 1)[0] + ";base64,"
        else:
            sanitized[key] = _without_embedded_image_data(item)
    return sanitized


def _estimate_context_tokens(system: object, messages: object, tools: object) -> int:
    value = _without_embedded_image_data(
        {"system": system, "messages": messages, "tools": tools}
    )
    return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))


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
        estimated_input = _estimate_context_tokens(system, messages, tools)
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
        original_history_ids = {
            str(identifier) for message in messages[:history_messages]
            for identifier in message.get("_history_turn_ids", ())
        }
        def refresh_episode_summary() -> None:
            summary = next(
                (message for message in messages if "_episode_summary_before" in message),
                None,
            )
            if summary is None:
                return
            retained = list(dict.fromkeys(
                str(turn_id)
                for message in messages[:history_messages]
                for turn_id in message.get("_history_turn_ids", ())
            ))
            episodes = self.store.compacted_episode_directory(
                retained,
                self.config.summary_results,
                before_timestamp=float(summary["_episode_summary_before"]),
            )
            rendered = recent_episode_lines(episodes, {})
            replacement = context_data_message(
                ("recent_episodes", rendered or "No episode summaries available."),
                required=True,
            )
            assert replacement is not None
            summary["content"] = replacement["content"]

        def size() -> int:
            return _estimate_context_tokens(system, messages, tools)

        # A dashboard/background change during a running Turn must be visible
        # before its next request, without rewriting the cached prefix.
        prefix = next((m for m in messages if "_memory_snapshot" in m), None)
        if prefix is not None:
            snapshot = prefix["_memory_snapshot"]
            state = self.store.transcript_memory_context(snapshot["turn_ids"], track_boundary=False)
            # Another executor may have compacted while this request was alive.
            # Replay the effective overrides rather than silently skipping a revision.
            if state["snapshot_revision"] > snapshot["revision"]:
                from ...storage.memory.memory_values import format_memory
                effective = "\n".join(
                    f'<replace id="{row["id"]}" revision="{state["revision"]}">'
                    + format_memory(row) + "</replace>"
                    for row in state["observed"].values()
                )
                messages.append({
                    "role": "user",
                    "content": "<memory_changes>\n"
                    + "\n".join(state["overrides"].values()) + "\n"
                    + effective + "\n</memory_changes>",
                    "_memory_change": state["revision"], "_context_prefix": True,
                })
                snapshot["revision"] = state["revision"]
            for event in state["events"]:
                if event["revision"] > snapshot["revision"]:
                    messages.append({
                        "role": "user", "content": event["content"],
                        "_memory_change": event["revision"], "_context_prefix": True,
                    })
            snapshot["revision"] = state["revision"]
            snapshot["current"] = self.store._memory_context([
                row for row in state["observed"].values() if row["activation"] == "always"
            ])
            snapshot["overrides"] = "\n".join(state["overrides"].values())

        hard_limit = self.config.max_input_tokens
        compaction_limit = min(hard_limit, context_compaction_tokens(self.config))
        refresh_episode_summary()
        estimated = size()
        dropped = 0
        truncated = 0
        compression_breakers = 0
        while estimated > compaction_limit and history_messages:
            start = next((i for i in range(history_messages)
                          if not messages[i].get("_context_prefix")), None)
            if start is None:
                break
            turn_ids = set(messages[start].get("_history_turn_ids", ()))
            if turn_ids:
                # Close over interleaved Turns and groups containing several Turns.
                # Removing a contiguous span preserves chronological/protocol order.
                end = start
                while True:
                    matches = [i for i in range(start, history_messages)
                               if turn_ids.intersection(messages[i].get("_history_turn_ids", ()))]
                    next_end = max(matches, default=end)
                    expanded = turn_ids.union(*(set(messages[i].get("_history_turn_ids", ()))
                                               for i in range(start, next_end + 1)))
                    if next_end == end and expanded == turn_ids:
                        break
                    end, turn_ids = next_end, expanded
                count = end - start + 1
                del messages[start:end + 1]
                history_messages -= count
                dropped += count
            else:
                messages.pop(start)
                history_messages -= 1
                dropped += 1
                while start < history_messages and (
                    messages[start].get("role") == "assistant"
                    or (isinstance(messages[start].get("content"), list)
                        and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                for block in messages[start]["content"]))
                    or "message delivery confirmation" in str(messages[start].get("content"))
                ):
                    messages.pop(start)
                    history_messages -= 1
                    dropped += 1
            refresh_episode_summary()
            estimated = size()
        if dropped:
            retained_ids = {
                str(identifier) for message in messages[:history_messages]
                for identifier in message.get("_history_turn_ids", ())
            }
            summary = next((m for m in messages if "_episode_summary_before" in m), None)
            prefix = next((m for m in messages if "_memory_snapshot" in m), None)
            if prefix is not None:
                self.store.persist_transcript_compaction(
                    original_history_ids - retained_ids,
                    summary["content"] if summary is not None else None,
                )
                snapshot = prefix["_memory_snapshot"]
                replacement = context_data_message(
                    ("long_term_memories", snapshot["current"]),
                    ("memory_overrides", snapshot["overrides"]),
                    ("goal_directory", snapshot["goals"]), required=True,
                )
                prefix["content"] = replacement["content"]
                self.store.fold_transcript_memory(snapshot["revision"])
                removed = sum(bool(m.get("_memory_change")) for m in messages[:history_messages])
                messages[:] = [m for m in messages if not m.get("_memory_change")]
                history_messages -= removed
                estimated = size()
        if estimated > compaction_limit:
            evidence_ids = {
                block.get("id") for message in messages
                for block in (message.get("content") if isinstance(message.get("content"), list) else [])
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in {"reply", "recall"}
            }
            for message in messages:
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        estimated <= compaction_limit
                        or not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                        or block.get("tool_use_id") in evidence_ids
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
