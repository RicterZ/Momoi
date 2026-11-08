"""Opt-in model navigation evaluation using real memory tools and synthetic data."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.models import AgentReply, ProviderResponse, ToolCall, TurnDraft
from momoi.storage import Store
from momoi.tools.contracts.memory import MEMORY_TOOL_POLICY, MEMORY_TOOL_SPECS
from momoi.tools.memory import MemoryTools
from .runner import ROOT, TEMP_ROOT, model_config, write_json

CASE = ROOT / "datasets/memory_navigation.json"


async def evaluate_navigation(complete, directory):
    case = json.loads(CASE.read_text(encoding="utf-8"))
    store = Store(directory / "memory.sqlite3")
    try:
        def seed(key, content):
            source = {"event_id": "synthetic", "quote": content}
            return store.memories.repository.write(
                {"kind": "preference", "key": key, "content": content, "activation": "recall",
                 "expires_at": None, "meta": {"tags": case["target"]["tags"]}},
                [], source, [source], now=1700000000,
            )
        for index, content in enumerate(case["distractors"]):
            seed(f"食物偏好{index}", content)
        target = seed(case["target"]["key"], case["target"]["content"])
        assert not store.memories.search_literal(case["input"], 10)
        catalog = store.memory_catalog_context()
        assert case["target"]["content"] not in catalog
        tools = [spec for spec in MEMORY_TOOL_SPECS
                 if spec["name"] in {"memory_search", "memory_browse", "memory_read"}]
        messages = [{"role": "user", "content": f"<memory_catalog>{catalog}</memory_catalog>\n{case['input']}"}]
        draft, trace = TurnDraft(), []
        executor = MemoryTools(store)
        store.begin_turn("eval", "owner", [])
        hit = False
        for _ in range(6):
            response = await complete(MEMORY_TOOL_POLICY, messages, tools)
            if not response.tool_calls:
                break
            messages.append({"role": "assistant", "content": response.content})
            observations = []
            for call in response.tool_calls:
                if call.name not in {spec["name"] for spec in tools}:
                    result = {"ok": False, "error": "tool_not_available"}
                else:
                    result = executor.execute(call, [], draft, turn_id="eval")
                rows = result.get("items", result.get("memories", []))
                hit |= (call.name in case["expected"]["navigation_tools"]
                        and result.get("ok", False) and any(row["id"] == target for row in rows))
                trace.append({"tool": call.name, "arguments": call.arguments, "result": result})
                observations.append({"type": "tool_result", "tool_use_id": call.id,
                                     "content": json.dumps(result, ensure_ascii=False)})
            messages.append({"role": "user", "content": observations})
            if hit:
                break
        store.commit_turn([], case["input"], AgentReply([]), draft, turn_id="eval")
        return {"id": case["id"], "status": "pass" if hit else "fail",
                "checks": {"browse_or_read_hit_target": hit}, "trace": trace,
                "target_access": store.memory_access_stats([target])[target]}
    finally:
        store.close()


async def scripted_complete(system, messages, tools):
    call = ToolCall("browse", "memory_browse", {"topic": "food_drink"})
    return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])


async def run(live):
    output = Path(tempfile.mkdtemp(prefix="momoi-eval-navigation-", dir=TEMP_ROOT))
    if live:
        async with OpenAIProvider(model_config("MOMOI_EVAL")) as provider:
            result = await evaluate_navigation(provider.complete, output)
    else:
        result = await evaluate_navigation(scripted_complete, output)
    result["mode"] = "live" if live else "scripted_smoke_only"
    write_json(output / "result.json", result)
    print(f"{result['status']} ({result['mode']}): {output}")
    return int(result["status"] != "pass")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Call the explicitly configured evaluation model")
    raise SystemExit(asyncio.run(run(parser.parse_args().live)))
