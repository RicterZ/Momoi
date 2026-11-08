from xml.etree.ElementTree import Element, SubElement

from ..memory_rendering import memory_element, owner_evidence_element, xml_sections


def render_memory_operation_request(*, now, timestamp,
                                    operations, visible, snapshots, evidence,
                                    goals=(), retrieval_fallback="", always_budget=1500, always_tokens=0):
    clock = Element("current_time", {"at": timestamp, "unix": str(now)})
    requests = Element("operation_requests")
    for operation in operations:
        attrs = {key: str(operation[key]) for key in ("id", "type", "target_id", "scope") if key in operation}
        request = SubElement(requests, "operation", attrs)
        SubElement(request, "content").text = operation["content"]
        SubElement(request, "evidence", {"event_id": operation["event_id"]}).text = operation["evidence"]
    memories = Element("current_memories")
    for memory_id, memory in snapshots.items():
        memories.append(memory_element(memory, visible=memory_id in visible))
    outdated = Element("outdated_visible_snapshots")
    for memory_id, memory in visible.items():
        if snapshots.get(memory_id) != memory:
            SubElement(outdated, "memory", {"id": str(memory_id), "status": "stale"})
    budget = Element("always_memory_budget", {"limit": str(always_budget), "used": str(always_tokens)})
    budget.text = "超预算的 always 写入必须提供 demote_ids，并使提交后的总量回到预算内；否则改用 recall 或 defer。"
    retrieval = Element("candidate_retrieval", {"fallback": retrieval_fallback})
    goal_directory = Element("goal_directory")
    for goal in goals:
        SubElement(goal_directory, "goal", {"id": str(goal["id"]), "scope": "goal:" + str(goal["id"]),
                                             "status": str(goal["status"])}).text = str(goal["title"])
    return xml_sections(clock, budget, requests, goal_directory, memories, outdated, retrieval,
                        owner_evidence_element(evidence))
