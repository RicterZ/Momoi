import asyncio

from evals.memory_navigation import evaluate_navigation, scripted_complete
from momoi.models import ProviderResponse, ToolCall


def test_navigation_eval_runs_real_tools_and_hits_synthetic_target(tmp_path):
    result = asyncio.run(evaluate_navigation(scripted_complete, tmp_path))
    assert result["status"] == "pass"
    assert result["target_access"]["counts"]["browsed"] == 1


def test_navigation_eval_rejects_search_only_even_when_it_hits_target(tmp_path):
    async def search_only(*args):
        call = ToolCall("search", "memory_search", {"query": "芫荽"})
        return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])
    result = asyncio.run(evaluate_navigation(search_only, tmp_path))
    assert result["status"] == "fail"
    assert result["trace"][0]["result"]["count"] == 1
