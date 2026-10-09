from xml.etree.ElementTree import Element, SubElement

from ..memory_rendering import memory_element, owner_evidence_element, xml_sections


def render_memory_operation_request(*, now, timestamp,
                                    operations, visible, snapshots, evidence,
                                    goals=(), forgotten=None, retrieval_fallback=""):
    clock = Element("current_time", {"at": timestamp, "unix": str(now)})
    requests = Element("operation_requests")
    for operation in operations:
        attrs = {key: str(operation[key]) for key in ("id", "type", "target_id") if key in operation}
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
    deleted = Element("forgotten_memories")
    for memory in (forgotten or {}).values():
        node = memory_element(memory)
        SubElement(node, "forgotten", {"at": str(memory["forgotten_at"]),
                                      "event_id": memory["forgotten_event_id"]}).text = memory["forgotten_quote"]
        deleted.append(node)
    retrieval = Element("candidate_retrieval", {"fallback": retrieval_fallback})
    goal_directory = Element("goal_directory")
    for goal in goals:
        SubElement(goal_directory, "goal", {"id": str(goal["id"]),
                                             "status": str(goal["status"])}).text = str(goal["title"])
    return xml_sections(clock, requests, goal_directory, memories, outdated, deleted, retrieval,
                        owner_evidence_element(evidence))
