"""Stable, compact observations for completed-turn replay only."""

import json
from ...tools.presentation import present_result
from collections.abc import Mapping


EDGE_CHARS = 80
SMALL_RESULT_CHARS = 600


def without_hashes(value):
    """Remove historical checksums, including recall's nested execution evidence."""
    if isinstance(value, dict):
        return {key: without_hashes(item) for key, item in value.items() if key != "sha256"}
    if isinstance(value, list):
        return [without_hashes(item) for item in value]
    return value


def preview(text: str) -> str:
    if len(text) <= EDGE_CHARS * 2:
        return text
    return text[:EDGE_CHARS] + "\n[...truncated...]\n" + text[-EDGE_CHARS:]


def list_excerpt(payload: Mapping) -> dict | None:
    """Recognize structured lists, including MCP structuredContent wrappers."""
    value = payload
    for _ in range(6):
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                return None
            items = []
            for item in value[:3]:
                entry = {k: preview(str(item[k])) for k in
                         ("id", "url", "title", "name", "created_at", "published_at") if k in item}
                user = item.get("user")
                if isinstance(user, dict) and user.get("screen_name"):
                    entry["author"] = preview(str(user["screen_name"]))
                elif item.get("author"):
                    entry["author"] = preview(str(item["author"]))
                body = item.get("text", item.get("content"))
                if isinstance(body, str):
                    entry["excerpt"] = preview(body)
                if not entry:
                    return None
                items.append(entry)
            return {"items": items, "shown": len(items), "returned_count": len(value),
                    "omitted": len(value) - len(items)}
        if not isinstance(value, dict):
            return None
        for key in ("structuredContent", "result", "items", "statuses", "data"):
            if isinstance(value.get(key), (dict, list)):
                value = value[key]
                break
        else:
            return None
    return None


def result_body(payload):
    """Unwrap result/structuredContent and text-only MCP blocks structurally."""
    value = payload
    for _ in range(12):
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                return value
            if not isinstance(decoded, (dict, list)):
                return value
            value = decoded
        elif isinstance(value, dict):
            if "result" in value:
                value = value["result"]
            elif "structuredContent" in value:
                value = value["structuredContent"]
            elif "content" in value:
                value = value["content"]
            else:
                return {key: item for key, item in value.items() if key not in {
                    "ok", "provenance", "result_ref", "sha256", "format", "original_chars",
                    "chunk_start", "chunk_end", "next_cursor", "has_more", "truncated",
                }}
        elif isinstance(value, list) and value and all(
            isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            for item in value
        ):
            value = "\n".join(item["text"] for item in value)
        else:
            return value
    return value


def historical_results(exchanges: list[dict], *, history_format: int = 3, result_store=None) -> None:
    """Edit a private replay copy; never alter the journal or live observations.

    Error runs are summarized in the first result, with paired references in
    subsequent results. Different errors remain distinguishable in the summary.
    """
    run: list[tuple[dict, dict]] = []
    run_name = ""

    def flush() -> None:
        if len(run) < 2:
            run.clear()
            return
        first_id = run[0][0].get("tool_use_id")
        errors: dict[str, int] = {}
        for _, payload in run:
            detail = preview(str(payload.get("error") or "tool_failed") + ": "
                             + str(payload.get("message") or ""))
            if history_format >= 3:
                diagnostic = {k: payload[k] for k in ("exit_code", "ambiguous", "upstream_error_type") if k in payload}
                for key in ("stderr_tail", "stderr"):
                    if payload.get(key):
                        diagnostic[key] = preview(str(payload[key]))
                if diagnostic:
                    detail += " " + json.dumps(diagnostic, ensure_ascii=False)
            errors[detail] = errors.get(detail, 0) + 1
        for index, (block, payload) in enumerate(run):
            compact = {"ok": False, "result_ref": payload.get("result_ref")}
            if index == 0:
                compact.update(error_run_count=len(run), errors=[
                    {"detail": detail, "count": count} for detail, count in errors.items()
                ])
            else:
                compact["error_summary_tool_use_id"] = first_id
            block["content"] = json.dumps(compact, ensure_ascii=False)
        run.clear()

    for exchange in exchanges:
        content = exchange.get("content")
        calls = {b.get("id"): b.get("name") for b in content
                 if isinstance(b, dict) and b.get("type") == "tool_use"} if isinstance(content, list) else {}
        if not calls:
            flush()
        for block in exchange.get("results", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                flush()
                continue
            raw = block.get("content")
            if not isinstance(raw, str):
                flush()
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                flush()
                continue
            if not isinstance(payload, Mapping):
                flush()
                continue
            if "provenance" in payload:
                payload = dict(payload)
                payload.pop("provenance")
                raw = json.dumps(payload, ensure_ascii=False)
                block["content"] = raw
            payload = present_result(dict(payload), historical=True)
            raw = json.dumps(payload, ensure_ascii=False)
            block["content"] = raw
            name = calls.get(block.get("tool_use_id"), "")
            # Recall evidence supports reuse; reply bubbles are the actual
            # conversation. Preserve both in full, including failure details.
            if name == "recall" and payload:
                payload = without_hashes(payload)
                block["content"] = json.dumps(payload, ensure_ascii=False)
            if name in {"recall", "reply"}:
                flush()
                run_name = ""
                continue
            # Paging metadata belongs to live continuation, not historical
            # excerpts. File hashes remain available for optimistic writes.
            if "chunk_start" in payload:
                payload = dict(payload)
                payload.pop("sha256", None)
                payload.pop("next_cursor", None)
                raw = json.dumps(payload, ensure_ascii=False)
                block["content"] = raw
            if "sha256" in payload:
                payload = dict(payload)
                payload.pop("sha256")
                raw = json.dumps(payload, ensure_ascii=False)
                block["content"] = raw
            if name and payload.get("ok") is False:
                if run_name != name:
                    flush()
                run_name = name
                run.append((block, dict(payload)))
            else:
                flush()
                run_name = ""
            if history_format >= 3 and payload.get("ok") is True:
                if name in {"reply", "send_bubbles", "end_turn"} and not payload.get("error") and not payload.get("truncated"):
                    allowed = {"ok", "error", "truncated", "provenance", "result_ref", "state", "channel", "bubbles"}
                    if set(payload) <= allowed:
                        block["content"] = json.dumps({k: payload[k] for k in ("ok", "state", "bubbles") if k in payload}, ensure_ascii=False)
                        continue
                if len(raw) <= SMALL_RESULT_CHARS:
                    compact = dict(payload)
                    for key, empty in (("error", None), ("truncated", False)):
                        if compact.get(key) == empty:
                            compact.pop(key, None)
                    compact.pop("provenance", None)
                    block["content"] = json.dumps(compact, ensure_ascii=False)
                    continue
            if len(raw) <= SMALL_RESULT_CHARS:
                continue
            compact = {key: payload[key] for key in (
                "ok", "error", "result_ref", "original_chars",
                "chunk_start", "chunk_end", "has_more",
            ) if key in payload}
            source = payload
            if "chunk_start" in payload and result_store is not None:
                snapshot = result_store.historical_payload(str(payload.get("result_ref") or ""))
                if isinstance(snapshot, dict):
                    source = snapshot
            body = result_body(source)
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            if history_format >= 3 and name == "read_file":
                lines = payload.get("lines")
                if isinstance(lines, list) and lines and all(
                    isinstance(line, dict) and isinstance(line.get("text"), str)
                    and isinstance(line.get("line"), int) for line in lines
                ):
                    body = "".join(line["text"] for line in lines)
                    compact.update(start_line=lines[0]["line"], end_line=lines[-1]["line"])
            compact.update(history_truncated=True, preview=preview(body))
            if history_format >= 3:
                if name == "exec":
                    for key in ("stdout_tail", "stderr_tail", "stdout", "stderr"):
                        if isinstance(payload.get(key), str):
                            compact[key] = preview(payload[key])
                    if any(key in compact for key in ("stdout_tail", "stderr_tail", "stdout", "stderr")):
                        compact.pop("preview", None)
                # Preserve control/outcome metadata independently of excerpt size.
                for key in ("state", "status", "delivery_state", "exit_code", "ambiguous", "connection_recovered",
                            "upstream_error_type", "plan_id", "step_id", "plan_status", "step_index",
                            "version", "operation_id", "image_id", "count", "pattern", "title",
                            "requested_url", "content_type", "extract_mode", "source_truncated",
                            "content_length"):
                    value = payload.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        compact[key] = preview(value) if isinstance(value, str) else value
                if payload.get("ok") is True:
                    compact.pop("provenance", None)
                    if compact.get("error") is None:
                        compact.pop("error", None)
                    listing = list_excerpt(payload) if name.startswith("mcp__weibo__") else None
                    if listing is not None:
                        compact.pop("preview", None)
                        compact.update(listing)
                for key in ("path", "start_line", "end_line", "total_lines", "content_offset", "next_content_offset", "sha256", "truncated", "url", "message"):
                    if key in payload:
                        compact[key] = preview(str(payload[key])) if isinstance(payload[key], str) else payload[key]
                if name in {"read_file", "read"} and "path" not in compact:
                    call = next((b for b in content if isinstance(b, dict) and b.get("id") == block.get("tool_use_id")), {})
                    path = (call.get("input") or {}).get("path")
                    if isinstance(path, str):
                        compact["path"] = preview(path)
            block["content"] = json.dumps(compact, ensure_ascii=False)
    flush()
